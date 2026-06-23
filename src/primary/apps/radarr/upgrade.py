#!/usr/bin/env python3
"""
Quality Upgrade Processing for Radarr
Handles searching for movies that need quality upgrades in Radarr
"""

import random
from typing import Dict, Any, Callable, Optional, Tuple
from src.primary.utils.logger import get_logger
from src.primary.apps.radarr import api as radarr_api
from src.primary.stats_manager import increment_stat
from src.primary.stateful_manager import is_processed, add_processed_id
from src.primary.utils.history_utils import log_processed_media

# Get logger for the app
radarr_logger = get_logger("radarr")


def _coerce_int(value: Any, default: int = 0) -> int:
    try:
        if value is None:
            return default
        return int(value)
    except (TypeError, ValueError):
        return default


def _get_release_preference(release_like: Dict[str, Any], profile_info: Dict[str, Any]) -> Optional[Tuple[int, int]]:
    quality_id = release_like.get("quality", {}).get("quality", {}).get("id")
    quality_rank = profile_info.get("quality_rank", {}).get(quality_id)

    if quality_rank is None:
        return None

    return quality_rank, _coerce_int(release_like.get("customFormatScore"), 0)


def _select_strict_upgrade_candidate(
    movie: Dict[str, Any], releases: list[Dict[str, Any]], profile_info: Dict[str, Any]
) -> Optional[Dict[str, Any]]:
    movie_id = movie.get("id")
    movie_file = movie.get("movieFile") or {}
    current_preference = _get_release_preference(movie_file, profile_info)
    if current_preference is None:
        radarr_logger.debug(f"Skipping movie ID {movie_id}: current file quality is not ranked in profile.")
        return None

    current_quality_rank, current_custom_format_score = current_preference
    cutoff_format_score = _coerce_int(profile_info.get("cutoff_format_score"), 0)
    min_upgrade_format_score = max(1, _coerce_int(profile_info.get("min_upgrade_format_score"), 1))

    best_candidate = None
    best_preference = None

    for release in releases:
        if not release.get("approved") or release.get("rejected") or not release.get("downloadAllowed"):
            continue

        candidate_preference = _get_release_preference(release, profile_info)
        if candidate_preference is None:
            continue

        candidate_quality_rank, candidate_custom_format_score = candidate_preference

        if candidate_quality_rank > current_quality_rank:
            is_valid_upgrade = True
        elif candidate_quality_rank == current_quality_rank:
            is_valid_upgrade = (
                current_custom_format_score < cutoff_format_score
                and candidate_custom_format_score >= current_custom_format_score + min_upgrade_format_score
            )
        else:
            is_valid_upgrade = False

        if is_valid_upgrade and (best_preference is None or candidate_preference > best_preference):
            best_candidate = release
            best_preference = candidate_preference

    return best_candidate


def process_cutoff_upgrades(
    app_settings: Dict[str, Any],
    stop_check: Callable[[], bool],  # Function to check if stop is requested
) -> bool:
    """
    Process quality cutoff upgrades for Radarr based on settings.

    Args:
        app_settings: Dictionary containing all settings for Radarr
        stop_check: A function that returns True if the process should stop

    Returns:
        True if any movies were processed for upgrades, False otherwise.
    """
    radarr_logger.info("Starting quality cutoff upgrades processing cycle for Radarr.")
    processed_any = False

    # Extract necessary settings
    api_url = app_settings.get("api_url", "").strip()
    api_key = app_settings.get("api_key", "").strip()
    api_timeout = app_settings.get("api_timeout", 120)
    monitored_only = app_settings.get("monitored_only", True)
    hunt_upgrade_movies = app_settings.get("hunt_upgrade_movies", 0)

    # Get instance name - check for instance_name first, fall back to legacy "name" key if needed
    instance_name = app_settings.get("instance_name", app_settings.get("name", "Radarr Default"))

    # Get movies eligible for upgrade
    radarr_logger.info("Retrieving movies eligible for cutoff upgrade...")
    upgrade_eligible_data = radarr_api.get_cutoff_unmet_movies(api_url, api_key, api_timeout, monitored_only)

    if not upgrade_eligible_data:
        radarr_logger.info("No movies found eligible for upgrade or error retrieving them.")
        return False

    profile_map = radarr_api.get_quality_profile_map(api_url, api_key, api_timeout)
    if profile_map is None:
        radarr_logger.info("Unable to build Radarr quality profile map. Skipping upgrades.")
        return False

    radarr_logger.info(f"Found {len(upgrade_eligible_data)} movies eligible for upgrade.")

    # Filter out already processed movies using stateful management
    unprocessed_movies = []
    for movie in upgrade_eligible_data:
        movie_id = str(movie.get("id"))
        if not is_processed("radarr", instance_name, movie_id):
            unprocessed_movies.append(movie)
        else:
            radarr_logger.debug(f"Skipping already processed movie ID: {movie_id}")

    radarr_logger.info(
        f"Found {len(unprocessed_movies)} unprocessed movies for upgrade out of {len(upgrade_eligible_data)} total."
    )

    if not unprocessed_movies:
        radarr_logger.info("No upgradeable movies found to process (after filtering already processed). Skipping.")
        return False

    radarr_logger.info("Randomizing upgrade candidate order before strict release preflight.")
    movies_to_process = unprocessed_movies[:]
    random.shuffle(movies_to_process)

    processed_count = 0
    processed_something = False

    for movie in movies_to_process:
        if processed_count >= hunt_upgrade_movies:
            break

        if stop_check():
            radarr_logger.info("Stop signal received, aborting Radarr upgrade cycle.")
            break

        movie_id = movie.get("id")
        movie_title = movie.get("title")
        movie_year = movie.get("year")
        profile_id = movie.get("qualityProfileId")
        profile_info = profile_map.get(profile_id)

        radarr_logger.info(f'Processing upgrade for movie: "{movie_title}" ({movie_year}) (Movie ID: {movie_id})')

        if profile_info is None:
            radarr_logger.warning(f"  - Skipping movie because quality profile {profile_id} could not be resolved.")
            continue

        releases = radarr_api.get_release_preflight(api_url, api_key, api_timeout, movie_id)
        if releases is None:
            radarr_logger.warning("  - Failed to retrieve release preflight data.")
            continue

        best_release = _select_strict_upgrade_candidate(movie, releases, profile_info)
        if best_release is None:
            radarr_logger.info("  - No strictly better downloadable release found in preflight results.")
            continue

        release_title = best_release.get("title", "Unknown Release")
        release_quality = best_release.get("quality", {}).get("quality", {}).get("name", "Unknown Quality")
        release_custom_format_score = _coerce_int(best_release.get("customFormatScore"), 0)
        radarr_logger.info(
            f"  - Selected release: {release_title} [{release_quality}] (custom format score: {release_custom_format_score})"
        )

        download_result = radarr_api.download_release(api_url, api_key, api_timeout, best_release)
        if download_result:
            radarr_logger.info("  - Successfully queued explicit release download.")
            add_processed_id("radarr", instance_name, str(movie_id))
            increment_stat("radarr", "upgraded")

            # Log to history so the upgrade appears in the history UI
            media_name = f"{movie_title} ({movie_year})"
            log_processed_media("radarr", media_name, movie_id, instance_name, "upgrade")
            radarr_logger.debug(f"Logged quality upgrade to history for movie ID {movie_id}")

            processed_count += 1
            processed_something = True
        else:
            radarr_logger.warning("  - Failed to queue explicit release download.")

    # Log final status
    radarr_logger.info(f"Completed processing {processed_count} movies for quality upgrades.")

    return processed_something
