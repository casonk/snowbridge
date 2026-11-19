# File Browser Portfolio Themes

File Browser can use the portfolio's shared colour themes (midnight, daylight,
ember, the contrast pair, hulk, neon, liberty and any you add on clockwork's
Themes page), so it matches the other private web pages.

## How it works

- `scripts/render_filebrowser_theme.py` copies the rendered portfolio
  `theme.css` and `theme.js` into one branding directory and appends a mapping
  from File Browser's own variables (`--background`, `--surfacePrimary`,
  `--textSecondary`, ...) to the portfolio tokens (`--bg`, `--surface`,
  `--text`, ...). Nothing in File Browser's frontend is recompiled for colours.
- File Browser's branding directory is a database setting, not an environment
  variable: `filebrowser config set -d /database/filebrowser.db --branding.files /config/branding`. It then
  always serves `custom.css` from there; the Snowbridge fork also serves
  `theme.js` and loads it before the app starts. A stock image therefore gets
  the CSS (it follows the system light/dark setting) but no rotator.
- `theme.js` adds a small rotator button at the bottom right. The choice is
  kept in this browser, and `?theme=ember` works on any URL. The "Everywhere"
  scope needs a `/_portfolio/global` route on this origin, which Snowbridge does
  not provide; it falls back to this site only.

## Install or refresh

Theme sources are taken from, in order, `--themes-dir` or
`SNOWBRIDGE_THEMES_DIR`, `~/.config/traction-control/themes/rendered` (what
clockwork's Themes page writes), then the sibling `traction-control/themes`.

```bash
python3 scripts/render_filebrowser_theme.py \
  --out "$FILEBROWSER_CONFIG_DIR/branding"
```

Run it again after editing themes. The files are plain static assets, so no
restart is needed; File Browser caches them for a day, so hard-reload the page.
The first time, rebuild the fork image (the fork's `main` carries
the `theme.js` change in `http/static.go` and `frontend/public/index.html`) and
recreate the compose stack, then stop it and set the branding directory once
(the database is locked while File Browser runs):

```bash
sudo podman-compose -f config/web/filebrowser/docker-compose.local.yml stop filebrowser
sudo podman-compose -f config/web/filebrowser/docker-compose.local.yml run --rm --no-deps \
  --entrypoint /bin/filebrowser filebrowser \
  config set -d /database/filebrowser.db --branding.files /config/branding
sudo podman-compose -f config/web/filebrowser/docker-compose.local.yml up -d filebrowser
```
