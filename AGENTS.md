# NeutArr production development fork

This checkout is mounted by the production `neutarr-dev` service. Follow
`/home/bradley/code/media-server-v2/AGENTS.md` for streaming checks and verified
maintenance mode before disruptive container operations. Avoid unreviewed
changes to files used by the running application.

- `origin`: https://github.com/bwsinger/NeutArr (personal GitHub fork).
- `upstream`: https://github.com/I-am-PUID-0/NeutArr (official project).
- Maintained branch: `media-server-main`; default push remote: `origin`.
- Use feature branches and separate PRs into the maintained fork branch.
- Existing local upgrade-decision and UI changes are retained in this branch.
  Check the diff against upstream before updates and preserve regression tests.
- Prefer equivalent upstream implementations over duplicate local patches;
  validate the behavior before removing the redundant custom implementation.
- Deployment lives in `media-server-v2/compose/mserver/neutarr-dev.yml`.
  Runtime configuration and credentials belong in appdata, never commits.
