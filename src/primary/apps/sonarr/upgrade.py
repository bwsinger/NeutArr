#!/usr/bin/env python3
"""
Sonarr cutoff upgrade processing module for NeutArr
"""

import time
import random
import json
from typing import List, Dict, Any, Callable, Union
from src.primary.utils.logger import get_logger
from src.primary.apps.sonarr import api as sonarr_api
from src.primary.stats_manager import increment_stat
from src.primary.stateful_manager import is_processed, add_processed_id
from src.primary.utils.history_utils import build_media_details, log_processed_media
from src.primary.settings_manager import get_advanced_setting

# Get logger for the Sonarr app
sonarr_logger = get_logger("sonarr")
DECISION_SAMPLE_LIMIT = 3


def _log_decision_event(event: str, **payload: Any) -> None:
    message = json.dumps({"event": event, **payload}, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    sonarr_logger.debug(f"DECISION_EVENT {message}")


def _season_decision_snapshot(
    series_id: int,
    season_number: int,
    cutoff_unmet_count: int,
    total_episodes: int,
    cutoff_unmet_percent: float,
    series_title: str,
) -> Dict[str, Any]:
    return {
        "series_id": series_id,
        "series_title": series_title,
        "season_number": season_number,
        "cutoff_unmet_count": cutoff_unmet_count,
        "total_episodes": total_episodes,
        "cutoff_unmet_percent": round(cutoff_unmet_percent, 1),
    }


def _filter_aired_episodes(episodes: List[Dict[str, Any]], context: str) -> List[Dict[str, Any]]:
    now_unix = time.time()
    aired_episodes: List[Dict[str, Any]] = []
    skipped_missing_air_date = 0
    skipped_future_air_date = 0
    skipped_invalid_air_date = 0

    for episode in episodes:
        air_date_utc = episode.get("airDateUtc")
        if not air_date_utc:
            skipped_missing_air_date += 1
            continue

        try:
            air_timestamp = time.mktime(time.strptime(air_date_utc, "%Y-%m-%dT%H:%M:%SZ"))
        except (TypeError, ValueError):
            skipped_invalid_air_date += 1
            continue

        if air_timestamp >= now_unix:
            skipped_future_air_date += 1
            continue

        aired_episodes.append(episode)

    skipped_count = skipped_missing_air_date + skipped_invalid_air_date + skipped_future_air_date
    if skipped_count:
        sonarr_logger.info(
            f"{context}: skipped {skipped_count} unaired or invalid episodes "
            f"({skipped_missing_air_date} missing air date, "
            f"{skipped_invalid_air_date} invalid air date, {skipped_future_air_date} future)."
        )
    else:
        sonarr_logger.debug(f"{context}: all {len(episodes)} episodes have aired.")

    return aired_episodes


def _sonarr_search_reason(episode: Dict[str, Any]) -> str:
    """Describe the cutoff condition Sonarr reported for a selected episode."""
    episode_file = episode.get("episodeFile") if isinstance(episode.get("episodeFile"), dict) else {}
    if episode.get("qualityCutoffNotMet") is True or episode_file.get("qualityCutoffNotMet") is True:
        return "Quality is below profile cutoff"
    return "Current file does not meet the Sonarr profile cutoff"


def _add_sonarr_search_context(episode_details: Dict[str, Any], cutoff_record: Dict[str, Any]) -> Dict[str, Any]:
    """Attach NeutArr-only search context without modifying Sonarr's API object."""
    details = dict(episode_details)
    details["_neutarr_search_context"] = {
        "search_reason": _sonarr_search_reason(cutoff_record),
    }
    return details


def process_cutoff_upgrades(
    api_url: str,
    api_key: str,
    instance_name: str,
    api_timeout: int = get_advanced_setting("api_timeout", 120),
    monitored_only: bool = True,
    # series_type: str = "standard",  # TODO: Add series type filtering (standard, daily, anime)
    hunt_upgrade_items: int = 5,
    upgrade_mode: str = "episodes",
    season_upgrade_min_cutoff_unmet_episodes: int = 3,
    season_upgrade_min_cutoff_unmet_percent: int = 40,
    command_wait_delay: int = get_advanced_setting("command_wait_delay", 1),
    command_wait_attempts: int = get_advanced_setting("command_wait_attempts", 600),
    stop_check: Callable[[], bool] = lambda: False,
) -> bool:
    """
    Process quality cutoff upgrades for Sonarr.
    This can use either episodes mode or shows mode for upgrades based on the upgrade_mode setting.
    """
    if hunt_upgrade_items <= 0:
        sonarr_logger.info("'hunt_upgrade_items' setting is 0 or less. Skipping upgrade processing.")
        return False

    sonarr_logger.info(f"Checking for {hunt_upgrade_items} quality upgrades...")

    sonarr_logger.info(f"Using {upgrade_mode.upper()} mode for quality upgrades")

    # Use the selected upgrade_mode
    if upgrade_mode == "seasons_packs":
        return process_upgrade_seasons_mode(
            api_url,
            api_key,
            instance_name,
            api_timeout,
            monitored_only,
            hunt_upgrade_items,
            season_upgrade_min_cutoff_unmet_episodes,
            season_upgrade_min_cutoff_unmet_percent,
            command_wait_delay,
            command_wait_attempts,
            stop_check,
        )
    else:  # Default to episodes mode
        return process_upgrade_episodes_mode(
            api_url,
            api_key,
            instance_name,
            api_timeout,
            monitored_only,
            hunt_upgrade_items,
            command_wait_delay,
            command_wait_attempts,
            stop_check,
        )


def process_upgrade_episodes_mode(
    api_url: str,
    api_key: str,
    instance_name: str,
    api_timeout: int,
    monitored_only: bool,
    hunt_upgrade_items: int,
    command_wait_delay: int,
    command_wait_attempts: int,
    stop_check: Callable[[], bool],
) -> bool:
    """Process upgrades in episode mode (original implementation)."""
    processed_any = False

    # For episodes mode, we want individual episode history entries
    skip_episode_history = False

    # Always use the efficient random page selection method
    sonarr_logger.debug(f"Using random selection for cutoff unmet episodes")
    episodes_to_search = sonarr_api.get_cutoff_unmet_episodes_random_page(
        api_url, api_key, api_timeout, monitored_only, hunt_upgrade_items
    )

    # If we didn't get enough episodes, we might need to try another page
    if len(episodes_to_search) < hunt_upgrade_items and len(episodes_to_search) > 0:
        sonarr_logger.debug(
            f"Got {len(episodes_to_search)} episodes from random page, fewer than requested {hunt_upgrade_items}"
        )

    if stop_check():
        sonarr_logger.info("Stop requested during upgrade processing.")
        return processed_any

    episodes_to_search = _filter_aired_episodes(episodes_to_search, "Episode mode aired filter")

    # Filter out already processed episodes for random selection approach
    unprocessed_episodes = []
    for episode in episodes_to_search:
        episode_id = str(episode.get("id"))
        if not is_processed("sonarr", instance_name, episode_id):
            unprocessed_episodes.append(episode)

    sonarr_logger.info(
        f"Found {len(unprocessed_episodes)} unprocessed cutoff unmet episodes out of {len(episodes_to_search)} total."
    )
    episodes_to_search = unprocessed_episodes

    if not episodes_to_search:
        sonarr_logger.info("No cutoff unmet episodes left to process for upgrades after filtering.")
        return False

    sonarr_logger.info(f"Selected {len(episodes_to_search)} cutoff unmet episodes to search for upgrades.")
    cutoff_records_by_id = {episode.get("id"): episode for episode in episodes_to_search}

    # Group episodes by series for potential refresh
    series_to_process: Dict[int, List[int]] = {}
    series_titles: Dict[int, str] = {}  # Store titles for logging
    for episode in episodes_to_search:
        series_id = episode.get("seriesId")
        if series_id:
            if series_id not in series_to_process:
                series_to_process[series_id] = []
                # Store title when first encountering the series ID
                series_titles[series_id] = episode.get("series", {}).get("title", f"Series ID {series_id}")
            series_to_process[series_id].append(episode["id"])

    # Process each series
    for series_id, episode_ids in series_to_process.items():
        if stop_check():
            sonarr_logger.info("Stop requested before processing next series for upgrades.")
            break

        series_title = series_titles.get(series_id, f"Series ID {series_id}")
        sonarr_logger.info(
            f"Processing series for upgrades: {series_title} (ID: {series_id}) with {len(episode_ids)} episodes."
        )

        if stop_check():
            sonarr_logger.info("Stop requested during episode processing for upgrades.")
            break

        # Trigger search for the selected episodes in this series
        sonarr_logger.debug(f"Attempting upgrade search for episode IDs: {episode_ids}")
        search_command_id = sonarr_api.search_episode(api_url, api_key, api_timeout, episode_ids)

        if search_command_id:
            # Wait for search command to complete
            if wait_for_command(
                api_url,
                api_key,
                api_timeout,
                search_command_id,
                command_wait_delay,
                command_wait_attempts,
                "Episode Upgrade Search",
                stop_check,
            ):
                # Mark episodes as processed if search command completed successfully
                processed_any = True  # Mark that we did something
                sonarr_logger.info(
                    f"Successfully processed and searched for {len(episode_ids)} episodes in series {series_id}."
                )

                # Add stats incrementing right here - this is the code path that's actually being executed
                for episode_id in episode_ids:
                    # Increment stat for each episode individually, just like Radarr
                    increment_stat("sonarr", "upgraded")
                    sonarr_logger.info(f"*** STATS INCREMENT *** sonarr upgraded by 1 for episode ID {episode_id}")

                # Mark episodes as processed using stateful management
                for episode_id in episode_ids:
                    add_processed_id("sonarr", instance_name, str(episode_id))
                    sonarr_logger.debug(f"Marked episode ID {episode_id} as processed for upgrades")

                    # Find the episode information for history logging
                    # We need to get the episode details from the API to include proper info in history
                    try:
                        episode_details = sonarr_api.get_episode(api_url, api_key, api_timeout, episode_id)
                        if episode_details:
                            series_title = episode_details.get("series", {}).get("title", "Unknown Series")
                            episode_title = episode_details.get("title", "Unknown Episode")
                            season_number = episode_details.get("seasonNumber", "Unknown Season")
                            episode_number = episode_details.get("episodeNumber", "Unknown Episode")

                            try:
                                season_episode = f"S{season_number:02d}E{episode_number:02d}"
                            except (ValueError, TypeError):
                                season_episode = f"S{season_number}E{episode_number}"

                            # Record the upgrade in history with quality upgrade identifier
                            media_name = f"{series_title} - {season_episode} - {episode_title}"
                            # Skip logging individual episodes since we log the season pack
                            if not skip_episode_history:
                                log_processed_media(
                                    "sonarr",
                                    media_name,
                                    episode_id,
                                    instance_name,
                                    "upgrade",
                                    build_media_details(
                                        "sonarr",
                                        _add_sonarr_search_context(
                                            episode_details,
                                            cutoff_records_by_id.get(episode_id, {}),
                                        ),
                                    ),
                                )
                            sonarr_logger.debug(f"Logged quality upgrade to history for episode ID {episode_id}")
                    except Exception as e:
                        sonarr_logger.error(f"Failed to log history for episode ID {episode_id}: {str(e)}")
            else:
                sonarr_logger.warning(
                    f"Episode upgrade search command (ID: {search_command_id}) for series {series_id} did not complete successfully or timed out. Episodes will not be marked as processed yet."
                )
        else:
            sonarr_logger.error(
                f"Failed to trigger upgrade search command for episodes {episode_ids} in series {series_id}."
            )

    sonarr_logger.info("Finished quality cutoff upgrades processing cycle for Sonarr.")
    return processed_any


def log_season_pack_upgrade(
    api_url: str, api_key: str, api_timeout: int, series_id: int, season_number: int, instance_name: str
):
    """Log a season pack upgrade to the history."""
    try:
        # Get series details for better history logging
        series_details = sonarr_api.get_series(api_url, api_key, api_timeout, series_id)
        if series_details:
            series_title = series_details.get("title", f"Series ID {series_id}")

            # Format season number for display
            try:
                season_id = f"S{season_number:02d}" if isinstance(season_number, int) else f"S{season_number}"
            except (ValueError, TypeError):
                season_id = f"S{season_number}"

            # Use the season ID directly - format as series_id + season number
            # This matches how Sonarr would identify a season
            season_id_num = f"{series_id}_{season_number}"

            # Create a descriptive name for the history entry
            media_name = f"{series_title} - {season_id} - COMPLETE SEASON PACK"

            # Log the season pack upgrade to history with normal 'upgrade' operation type
            details = build_media_details(
                "sonarr",
                {
                    "series": series_details,
                    "seasonNumber": season_number,
                },
                "Season pack",
            )
            details["search_reason"] = "Season contains episodes that do not meet their Sonarr profile cutoff"
            log_processed_media(
                "sonarr",
                media_name,
                season_id_num,
                instance_name,
                "upgrade",
                details,
            )
            sonarr_logger.debug(f"Logged season pack upgrade to history for {series_title} Season {season_number}")
    except Exception as e:
        sonarr_logger.error(f"Failed to log season pack upgrade to history: {str(e)}")


def process_upgrade_seasons_mode(
    api_url: str,
    api_key: str,
    instance_name: str,
    api_timeout: int,
    monitored_only: bool,
    hunt_upgrade_items: int,
    season_upgrade_min_cutoff_unmet_episodes: int,
    season_upgrade_min_cutoff_unmet_percent: int,
    command_wait_delay: int,
    command_wait_attempts: int,
    stop_check: Callable[[], bool],
) -> bool:
    """Process upgrades in season mode - groups episodes by season."""
    processed_any = False

    # Flag to skip individual episode history logging since we log the whole season pack
    skip_episode_history = True

    # Use the efficient random page selection method to get a sample of cutoff unmet episodes
    sonarr_logger.debug(f"Using random page selection for cutoff unmet episodes")
    # Request slightly more episodes than needed to ensure we have enough for a few seasons
    sample_size = hunt_upgrade_items * 10
    cutoff_unmet_episodes = sonarr_api.get_cutoff_unmet_episodes_random_page(
        api_url, api_key, api_timeout, monitored_only, sample_size
    )

    sonarr_logger.info(
        f"Received {len(cutoff_unmet_episodes)} cutoff unmet episodes from random page (before filtering)."
    )

    if not cutoff_unmet_episodes:
        sonarr_logger.info("No cutoff unmet episodes found in Sonarr.")
        return False

    cutoff_unmet_episodes = _filter_aired_episodes(cutoff_unmet_episodes, "Season-pack sample aired filter")

    if stop_check():
        sonarr_logger.info("Stop requested during upgrade processing.")
        return processed_any

    season_upgrade_min_cutoff_unmet_episodes = max(1, int(season_upgrade_min_cutoff_unmet_episodes))
    season_upgrade_min_cutoff_unmet_percent = max(0, min(100, int(season_upgrade_min_cutoff_unmet_percent)))
    sonarr_logger.info(
        "Using season-pack upgrade thresholds: "
        f"{season_upgrade_min_cutoff_unmet_episodes} cutoff-unmet episodes and "
        f"{season_upgrade_min_cutoff_unmet_percent}% cutoff-unmet."
    )

    candidate_series_ids = []
    seen_series_ids = set()
    for episode in cutoff_unmet_episodes:
        series_id = episode.get("seriesId")
        if series_id is not None and series_id not in seen_series_ids:
            candidate_series_ids.append(series_id)
            seen_series_ids.add(series_id)

    available_seasons = []
    season_cutoff_unmet_episode_map: Dict[tuple[int, int], List[Dict[str, Any]]] = {}
    skipped_counts = {"min_cutoff_unmet_episodes": 0, "min_cutoff_unmet_percent": 0}
    skipped_examples = {"min_cutoff_unmet_episodes": [], "min_cutoff_unmet_percent": []}
    considered_season_count = 0

    for series_id in candidate_series_ids:
        all_series_cutoff_unmet = sonarr_api.get_cutoff_unmet_episodes_for_series(
            api_url, api_key, api_timeout, series_id, monitored_only
        )
        all_series_cutoff_unmet = _filter_aired_episodes(
            all_series_cutoff_unmet, f"Season-pack series cutoff-unmet filter series={series_id}"
        )

        if not all_series_cutoff_unmet:
            continue

        all_series_episodes = sonarr_api.get_episodes_for_series(
            api_url, api_key, api_timeout, series_id, monitored_only
        )
        all_series_episodes = _filter_aired_episodes(
            all_series_episodes, f"Season-pack series full-episode filter series={series_id}"
        )

        if not all_series_episodes:
            continue

        seasons_by_number: Dict[int, Dict[str, Any]] = {}

        for episode in all_series_episodes:
            season_number = episode.get("seasonNumber")
            if season_number is None:
                continue

            if season_number not in seasons_by_number:
                seasons_by_number[season_number] = {
                    "series_title": episode.get("series", {}).get("title", f"Series ID {series_id}"),
                    "total_episodes": 0,
                    "cutoff_unmet_episodes": [],
                }

            seasons_by_number[season_number]["total_episodes"] += 1

        for episode in all_series_cutoff_unmet:
            season_number = episode.get("seasonNumber")
            if season_number is None:
                continue

            if season_number not in seasons_by_number:
                seasons_by_number[season_number] = {
                    "series_title": episode.get("series", {}).get("title", f"Series ID {series_id}"),
                    "total_episodes": 0,
                    "cutoff_unmet_episodes": [],
                }

            seasons_by_number[season_number]["cutoff_unmet_episodes"].append(episode)

        for season_number, season_data in seasons_by_number.items():
            total_episodes = season_data["total_episodes"]
            cutoff_unmet_for_season = season_data["cutoff_unmet_episodes"]
            cutoff_unmet_count = len(cutoff_unmet_for_season)

            if total_episodes <= 0 or cutoff_unmet_count <= 0:
                continue

            considered_season_count += 1
            cutoff_unmet_percent = (cutoff_unmet_count / total_episodes) * 100
            series_title = season_data["series_title"]

            if cutoff_unmet_count < season_upgrade_min_cutoff_unmet_episodes:
                skipped_counts["min_cutoff_unmet_episodes"] += 1
                if len(skipped_examples["min_cutoff_unmet_episodes"]) < DECISION_SAMPLE_LIMIT:
                    skipped_examples["min_cutoff_unmet_episodes"].append(
                        _season_decision_snapshot(
                            series_id,
                            season_number,
                            cutoff_unmet_count,
                            total_episodes,
                            cutoff_unmet_percent,
                            series_title,
                        )
                    )
                continue

            if cutoff_unmet_percent < season_upgrade_min_cutoff_unmet_percent:
                skipped_counts["min_cutoff_unmet_percent"] += 1
                if len(skipped_examples["min_cutoff_unmet_percent"]) < DECISION_SAMPLE_LIMIT:
                    skipped_examples["min_cutoff_unmet_percent"].append(
                        _season_decision_snapshot(
                            series_id,
                            season_number,
                            cutoff_unmet_count,
                            total_episodes,
                            cutoff_unmet_percent,
                            series_title,
                        )
                    )
                continue

            season_cutoff_unmet_episode_map[(series_id, season_number)] = cutoff_unmet_for_season
            available_seasons.append(
                (series_id, season_number, cutoff_unmet_count, total_episodes, cutoff_unmet_percent, series_title)
            )

    if not available_seasons:
        _log_decision_event(
            "sonarr_season_pack_candidates",
            result="no_candidates",
            candidate_series_count=len(candidate_series_ids),
            considered_season_count=considered_season_count,
            thresholds={
                "min_cutoff_unmet_episodes": season_upgrade_min_cutoff_unmet_episodes,
                "min_cutoff_unmet_percent": season_upgrade_min_cutoff_unmet_percent,
            },
            skipped_counts=skipped_counts,
            skipped_examples=skipped_examples,
            eligible_count=0,
            selected_seasons=[],
        )
        sonarr_logger.info("No valid seasons with cutoff unmet episodes met the configured thresholds.")
        return False

    # Select seasons to process - always randomly
    random.shuffle(available_seasons)
    seasons_to_process = available_seasons[:hunt_upgrade_items]

    sonarr_logger.info(f"Selected {len(seasons_to_process)} seasons with cutoff unmet episodes to process")
    _log_decision_event(
        "sonarr_season_pack_candidates",
        result="selected",
        candidate_series_count=len(candidate_series_ids),
        considered_season_count=considered_season_count,
        thresholds={
            "min_cutoff_unmet_episodes": season_upgrade_min_cutoff_unmet_episodes,
            "min_cutoff_unmet_percent": season_upgrade_min_cutoff_unmet_percent,
        },
        skipped_counts=skipped_counts,
        skipped_examples=skipped_examples,
        eligible_count=len(available_seasons),
        selected_seasons=[
            _season_decision_snapshot(
                series_id,
                season_number,
                episode_count,
                total_episodes,
                cutoff_unmet_percent,
                series_title,
            )
            for series_id, season_number, episode_count, total_episodes, cutoff_unmet_percent, series_title in seasons_to_process
        ],
    )

    # Process each selected season
    for series_id, season_number, _, _, _, series_title in seasons_to_process:
        if stop_check():
            sonarr_logger.info("Stop requested during season processing.")
            break

        episodes = season_cutoff_unmet_episode_map.get((series_id, season_number), [])
        episode_ids = [episode["id"] for episode in episodes]

        sonarr_logger.info(
            f"Processing {series_title} - Season {season_number} with {len(episode_ids)} cutoff unmet episodes"
        )

        if stop_check():
            sonarr_logger.info("Stop requested during season processing.")
            break

        # Trigger search for the entire season instead of individual episodes
        sonarr_logger.debug(f"Attempting to search for entire Season {season_number} of {series_title} for upgrades")
        search_command_id = sonarr_api.search_season(api_url, api_key, api_timeout, series_id, season_number)

        if search_command_id:
            # Wait for search command to complete
            if wait_for_command(
                api_url,
                api_key,
                api_timeout,
                search_command_id,
                command_wait_delay,
                command_wait_attempts,
                "Episode Upgrade Search",
                stop_check,
            ):
                # Mark as processed if search command completed successfully
                processed_any = True
                sonarr_logger.info(
                    f"Successfully triggered season pack search for {series_title} Season {season_number} with {len(episode_ids)} cutoff unmet episodes"
                )

                # Log this as a season pack upgrade in the history
                log_season_pack_upgrade(api_url, api_key, api_timeout, series_id, season_number, instance_name)

                # We'll increment stats individually for each episode instead of in batch
                # increment_stat("sonarr", "upgraded", len(episode_ids))
                # sonarr_logger.debug(f"Incremented sonarr upgraded statistics by {len(episode_ids)}")

                # Mark episodes as processed using stateful management
                for episode_id in episode_ids:
                    add_processed_id("sonarr", instance_name, str(episode_id))
                    sonarr_logger.debug(f"Marked episode ID {episode_id} as processed for upgrades")

                    # Increment stats for this episode (consistent with Radarr's approach)
                    increment_stat("sonarr", "upgraded")
                    sonarr_logger.debug(f"Incremented sonarr upgraded statistic for episode {episode_id}")

                    # Find the episode information for history logging
                    # We need to get the episode details from the API to include proper info in history
                    try:
                        episode_details = sonarr_api.get_episode(api_url, api_key, api_timeout, episode_id)
                        if episode_details:
                            series_title = episode_details.get("series", {}).get("title", "Unknown Series")
                            episode_title = episode_details.get("title", "Unknown Episode")
                            season_number = episode_details.get("seasonNumber", "Unknown Season")
                            episode_number = episode_details.get("episodeNumber", "Unknown Episode")

                            try:
                                season_episode = f"S{season_number:02d}E{episode_number:02d}"
                            except (ValueError, TypeError):
                                season_episode = f"S{season_number}E{episode_number}"

                            # Record the upgrade in history with quality upgrade identifier
                            media_name = f"{series_title} - {season_episode} - {episode_title}"
                            # Skip logging individual episodes since we log the season pack
                            if not skip_episode_history:
                                log_processed_media(
                                    "sonarr",
                                    media_name,
                                    episode_id,
                                    instance_name,
                                    "upgrade",
                                    build_media_details("sonarr", episode_details),
                                )
                            sonarr_logger.debug(f"Logged quality upgrade to history for episode ID {episode_id}")
                    except Exception as e:
                        sonarr_logger.error(f"Failed to log history for episode ID {episode_id}: {str(e)}")
            else:
                sonarr_logger.warning(
                    f"Season pack search command for {series_title} Season {season_number} did not complete successfully"
                )
        else:
            sonarr_logger.error(
                f"Failed to trigger season pack search command for {series_title} Season {season_number}"
            )

    sonarr_logger.info("Finished quality cutoff upgrades processing cycle (season mode) for Sonarr.")
    return processed_any


def process_upgrade_shows_mode(
    api_url: str,
    api_key: str,
    instance_name: str,
    api_timeout: int,
    monitored_only: bool,
    hunt_upgrade_items: int,
    command_wait_delay: int,
    command_wait_attempts: int,
    stop_check: Callable[[], bool],
) -> bool:
    """Process upgrades in show mode - gets all cutoff unmet episodes for entire shows."""
    processed_any = False

    # For shows mode, we want individual episode history entries
    skip_episode_history = False

    # Use the efficient random page selection method to get a sample of cutoff unmet episodes
    sonarr_logger.debug(f"Using random page selection for cutoff unmet episodes in shows mode")
    # Request slightly more episodes than needed to ensure we have enough for a few shows
    sample_size = hunt_upgrade_items * 20  # Use a larger multiplier for shows mode
    cutoff_unmet_sample = sonarr_api.get_cutoff_unmet_episodes_random_page(
        api_url, api_key, api_timeout, monitored_only, sample_size
    )

    sonarr_logger.info(
        f"Received {len(cutoff_unmet_sample)} cutoff unmet episodes from random page (before filtering)."
    )

    if not cutoff_unmet_sample:
        sonarr_logger.info("No cutoff unmet episodes found in Sonarr.")
        return False

    cutoff_unmet_sample = _filter_aired_episodes(cutoff_unmet_sample, "Shows-mode sample aired filter")

    if stop_check():
        sonarr_logger.info("Stop requested during upgrade processing.")
        return processed_any

    # Group episodes by series to identify candidate shows
    series_info: Dict[int, Dict] = {}  # Store series ID -> {title, sample_count}

    for episode in cutoff_unmet_sample:
        series_id = episode.get("seriesId")
        if series_id is not None:
            if series_id not in series_info:
                series_info[series_id] = {
                    "title": episode.get("series", {}).get("title", f"Series ID {series_id}"),
                    "sample_count": 0,
                }
            series_info[series_id]["sample_count"] += 1

    # Get list of candidate series from the sample
    series_candidates = []
    for series_id, info in series_info.items():
        series_candidates.append((series_id, info["sample_count"], info["title"]))

    if not series_candidates:
        sonarr_logger.info("No valid series with cutoff unmet episodes found in sample.")
        return False

    # Randomly select up to hunt_upgrade_items series to process
    random.shuffle(series_candidates)
    series_to_process = series_candidates[:hunt_upgrade_items]

    sonarr_logger.info(f"Selected {len(series_to_process)} series with cutoff unmet episodes to process")

    # Process each selected series
    for series_id, _, series_title in series_to_process:
        if stop_check():
            sonarr_logger.info("Stop requested before processing next series.")
            break

        # Get ALL cutoff unmet episodes for this series (not just the ones in the sample)
        all_series_episodes = sonarr_api.get_cutoff_unmet_episodes_for_series(
            api_url, api_key, api_timeout, series_id, monitored_only
        )

        all_series_episodes = _filter_aired_episodes(all_series_episodes, f"Shows-mode aired filter for {series_title}")

        episode_ids = [episode["id"] for episode in all_series_episodes]
        cutoff_records_by_id = {episode.get("id"): episode for episode in all_series_episodes}

        if not episode_ids:
            sonarr_logger.warning(f"No valid episodes found for {series_title} after filtering")
            continue

        sonarr_logger.info(f"Processing {series_title} with {len(episode_ids)} cutoff unmet episodes")

        if stop_check():
            sonarr_logger.info("Stop requested during show processing.")
            break

        # Trigger search for all cutoff unmet episodes in this series
        sonarr_logger.debug(f"Attempting to search for {len(episode_ids)} episodes in {series_title} for upgrades")
        search_command_id = sonarr_api.search_episode(api_url, api_key, api_timeout, episode_ids)

        if search_command_id:
            # Wait for search command to complete
            if wait_for_command(
                api_url,
                api_key,
                api_timeout,
                search_command_id,
                command_wait_delay,
                command_wait_attempts,
                "Episode Upgrade Search",
                stop_check,
            ):
                # Mark as processed if search command completed successfully
                processed_any = True
                sonarr_logger.info(f"Successfully processed {len(episode_ids)} cutoff unmet episodes in {series_title}")

                # We'll increment stats individually for each episode instead of in batch
                # increment_stat("sonarr", "upgraded", len(episode_ids))
                # sonarr_logger.debug(f"Incremented sonarr upgraded statistics by {len(episode_ids)}")

                # Mark episodes as processed using stateful management
                for episode_id in episode_ids:
                    add_processed_id("sonarr", instance_name, str(episode_id))
                    sonarr_logger.debug(f"Marked episode ID {episode_id} as processed for upgrades")

                    # Increment stats for this episode (consistent with Radarr's approach)
                    increment_stat("sonarr", "upgraded")
                    sonarr_logger.debug(f"Incremented sonarr upgraded statistic for episode {episode_id}")

                    # Find the episode information for history logging
                    # We need to get the episode details from the API to include proper info in history
                    try:
                        episode_details = sonarr_api.get_episode(api_url, api_key, api_timeout, episode_id)
                        if episode_details:
                            series_title = episode_details.get("series", {}).get("title", "Unknown Series")
                            episode_title = episode_details.get("title", "Unknown Episode")
                            season_number = episode_details.get("seasonNumber", "Unknown Season")
                            episode_number = episode_details.get("episodeNumber", "Unknown Episode")

                            try:
                                season_episode = f"S{season_number:02d}E{episode_number:02d}"
                            except (ValueError, TypeError):
                                season_episode = f"S{season_number}E{episode_number}"

                            # Record the upgrade in history with quality upgrade identifier
                            media_name = f"{series_title} - {season_episode} - {episode_title}"
                            # Skip logging individual episodes since we log the season pack
                            if not skip_episode_history:
                                log_processed_media(
                                    "sonarr",
                                    media_name,
                                    episode_id,
                                    instance_name,
                                    "upgrade",
                                    build_media_details(
                                        "sonarr",
                                        _add_sonarr_search_context(
                                            episode_details,
                                            cutoff_records_by_id.get(episode_id, {}),
                                        ),
                                    ),
                                )
                            sonarr_logger.debug(f"Logged quality upgrade to history for episode ID {episode_id}")
                    except Exception as e:
                        sonarr_logger.error(f"Failed to log history for episode ID {episode_id}: {str(e)}")
            else:
                sonarr_logger.warning(
                    f"Episode upgrade search command for {series_title} did not complete successfully"
                )
        else:
            sonarr_logger.error(f"Failed to trigger upgrade search command for {series_title}")

    sonarr_logger.info("Finished quality cutoff upgrades processing cycle (show mode) for Sonarr.")
    return processed_any


def wait_for_command(
    api_url: str,
    api_key: str,
    api_timeout: int,
    command_id: Union[int, str],
    wait_delay: int,
    max_attempts: int,
    command_name: str = "Command",
    stop_check: Callable[[], bool] = lambda: False,
) -> bool:
    """
    Wait for a Sonarr command to complete or timeout.

    Args:
        api_url: The Sonarr API URL
        api_key: The Sonarr API key
        api_timeout: API request timeout
        command_id: The ID of the command to monitor
        wait_delay: Seconds to wait between status checks
        max_attempts: Maximum number of status check attempts
        command_name: Name of the command (for logging)
        stop_check: Optional function to check if operation should be aborted

    Returns:
        True if command completed successfully, False otherwise
    """
    if wait_delay <= 0 or max_attempts <= 0:
        sonarr_logger.debug(
            f"Not waiting for command to complete (wait_delay={wait_delay}, max_attempts={max_attempts})"
        )
        return True  # Return as if successful since we're not checking

    sonarr_logger.debug(
        f"Waiting for {command_name} to complete (command ID: {command_id}). Checking every {wait_delay}s for up to {max_attempts} attempts"
    )

    # Wait for command completion
    attempts = 0
    while attempts < max_attempts:
        if stop_check():
            sonarr_logger.info(f"Stopping wait for {command_name} due to stop request")
            return False

        command_status = sonarr_api.get_command_status(api_url, api_key, api_timeout, command_id)

        if command_status is None:
            sonarr_logger.warning(
                f"Failed to get status for {command_name} (ID: {command_id}), attempt {attempts + 1}. "
                f"Command may have already completed or the ID is no longer valid."
            )
            return False  # Stop polling if command ID is not found or error occurs

        status = command_status.get("status")
        if status == "completed":
            sonarr_logger.debug(f"Sonarr {command_name} (ID: {command_id}) completed successfully")
            return True
        elif status in ["failed", "aborted"]:
            sonarr_logger.warning(f"Sonarr {command_name} (ID: {command_id}) {status}")
            return False

        sonarr_logger.debug(
            f"Sonarr {command_name} (ID: {command_id}) status: {status}, attempt {attempts + 1}/{max_attempts}"
        )

        attempts += 1
        time.sleep(wait_delay)

    sonarr_logger.error(f"Sonarr command '{command_name}' (ID: {command_id}) timed out after {max_attempts} attempts.")
    return False
