# Jellyfin Home Manager

A small, standalone admin UI for managing the Jellyfin home-library layout for every active user. It talks only to Jellyfin's native HTTP API and has no database or runtime dependencies beyond Python.

## What it does

- Discovers current libraries directly from `GET /Library/VirtualFolders`, including their names and IDs.
- Refreshes safely and reports renamed, new, and removed libraries. Existing order and toggles are retained; new libraries are appended and enabled by default.
- Provides drag-and-drop ordering and independent **My Media** / **Latest** visibility for each library.
- Reads each user's accessible views from `GET /Users/{id}/Views`, then removes inaccessible libraries from that user's generated layout while preserving the master order.
- Lets you edit a master set of native Jellyfin user preferences.
- Previews the effective library order for every active user.
- Before apply, writes a complete timestamped user backup. After every update it reads the user back from Jellyfin and verifies all managed fields.
- Runs rootless with Podman, as an unprivileged user with all Linux capabilities dropped.

Disabled users are skipped. The application never stores the Jellyfin API key in its data directory.

## Jellyfin setup

1. In Jellyfin Dashboard, create an API key under **Advanced → API Keys**. The key must belong to an administrator context capable of listing and updating users.
2. Export `JELLYFIN_URL` and `JELLYFIN_API_KEY` in the shell or service environment that starts the container.
3. If Jellyfin uses a private CA, mount the CA into the container and configure the Python trust store. `JELLYFIN_VERIFY_TLS=false` is available for testing but is not recommended.

## Rootless Podman deployment

```sh
export JELLYFIN_URL=http://jellyfin.example:8096
export JELLYFIN_API_KEY=replace-with-an-admin-api-key
export JELLYFIN_VERIFY_TLS=true
export JHM_PORT=8080

mkdir -p data
podman unshare chown 10001:10001 data
podman compose up -d --build
```

Open `http://127.0.0.1:8080`. The default binding is loopback-only because this is an administrative tool. Put it behind your authenticated reverse proxy if remote access is required.

To use Podman's systemd integration, generate or write a Quadlet from the built image; keep the `/data` volume owned by container UID 10001 and pass the same environment values.

## GitHub Container Registry package

The included `.github/workflows/container.yml` workflow builds an OCI-compatible image with Buildah and publishes it as a GitHub package at:

```text
ghcr.io/OWNER/REPOSITORY
```

It runs for pull requests as a build check without publishing. Pushes to `main` publish `latest` and `sha-XXXXXXXXXXXX`; tags beginning with `v` publish the version without the leading `v` plus the commit tag. It can also be run manually from the Actions tab.

The workflow uses the repository's automatic `GITHUB_TOKEN`; no registry secret is needed. In **Settings → Actions → General → Workflow permissions**, allow read and write permissions if the repository or organization overrides the workflow's declared `packages: write` permission.

Pull and run the package with rootless Podman:

```sh
podman pull ghcr.io/OWNER/REPOSITORY:latest
podman run -d --name jellyfin-home-manager \
  --user 10001:10001 \
  --cap-drop=all \
  --security-opt=no-new-privileges \
  -e JELLYFIN_URL \
  -e JELLYFIN_API_KEY \
  -e JELLYFIN_VERIFY_TLS \
  -p 127.0.0.1:8080:8080 \
  -v ./data:/data:Z \
  ghcr.io/OWNER/REPOSITORY:latest
```

GitHub packages inherit repository visibility by default. For a private package, authenticate before pulling with `podman login ghcr.io` using a token that has `read:packages`.

## Workflow

1. Select **Refresh libraries** to reconcile the saved template with live Jellyfin data.
2. Drag libraries into order and choose which appear in **My Media** and **Latest**.
3. Adjust the master settings and save the template.
4. Select **Preview apply to all**. Each preview is already filtered to that user's accessible views.
5. Apply. The result reports each user's read-back verification and the backup filename.

Backups are JSON files in `data/backups/`. They contain Jellyfin user records and should be treated as sensitive. To restore, use the saved `Configuration` object with Jellyfin's `POST /Users/{id}/Configuration` endpoint after reviewing it.

## Native API endpoints used

- `GET /Library/VirtualFolders`
- `GET /Users`
- `GET /Users/{id}`
- `GET /Users/{id}/Views`
- `POST /Users/{id}/Configuration`

Requests authenticate with `X-Emby-Token`.

## Run without a container

Python 3.11 or newer is recommended.

```sh
export JELLYFIN_URL=http://127.0.0.1:8096
export JELLYFIN_API_KEY=your-key
python app.py
```

## Tests

The test suite uses an in-memory mock Jellyfin adapter and does not need a real server or network access.

```sh
python -m unittest discover -s tests -v
```

## Operational notes

- Configuration writes are atomic and permissions are restricted to the container user.
- Apply is intentionally sequential so verification and per-user failures are clear.
- A partial failure returns HTTP 207 and leaves the pre-change backup intact.
- Library access is evaluated live for every preview and apply, not cached from discovery.
