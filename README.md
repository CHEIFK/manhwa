# Manwa Manager

A portable local and GitHub Codespaces manga / manhwa library manager, downloader, and reader.

## Repository Structure

```
manhwa/
├── .github/
│   └── workflows/
│       └── build-appimage.yml      # GitHub Actions CI workflow for AppImage builds & releases
│
├── manwa_manager/
│   ├── .devcontainer/
│   │   └── devcontainer.json       # Codespaces dev container configuration
│   ├── assets/
│   │   └── manwamanager.svg        # App icon
│   ├── scripts/
│   │   └── AppRun                  # AppImage runtime entrypoint
│   ├── app.py                      # Main backend server & library logic
│   ├── build-appimage.sh           # Local AppImage build script
│   ├── index.html                  # Web UI (Reader, Downloader, Library)
│   ├── ManwaManager.desktop        # Desktop entry configuration
│   ├── ManwaManager.spec           # PyInstaller build specification
│   ├── README.txt                  # Application documentation
│   ├── requirements.txt            # Python dependencies
│   └── run.sh                      # Quick start script
│
├── .gitignore
└── README.md
```

## Running Locally

1. Install dependencies:
   ```bash
   pip install -r manwa_manager/requirements.txt
   ```
2. Start the application:
   ```bash
   ./manwa_manager/run.sh
   ```
   Or from inside `manwa_manager`:
   ```bash
   python3 app.py
   ```
3. Open your browser at:
   ```
   http://127.0.0.1:8765
   ```

## GitHub Codespaces

When launching a Codespace:
- Uses the Python 3.12 devcontainer.
- Automatically installs `requirements.txt`.
- Forwards port `8765` and opens the browser.
- Starts the application server automatically.

## Library & Storage

- User settings and the chosen library directory are saved in `~/.config/ManwaManager/config.json`.
- The library default is `~/Manwa Library`, and it can be changed at any time from the Web UI.
- All downloaded chapters and images remain outside the AppImage / git repository.

## Building the AppImage

### On GitHub Actions:
- **Manual Trigger**: Run the `Build AppImage` workflow under the GitHub Actions tab.
- **Automatic Trigger**: Triggers automatically on pushes to `manwa_manager/**`.
- **Release Trigger**: When a version tag (e.g. `v1.0.0`) is pushed, the AppImage is built and attached to the GitHub Release.

### Locally:
```bash
./manwa_manager/build-appimage.sh
```
The output will be generated as `manwa_manager/ManwaManager-x86_64.AppImage`.