MANWA MANAGER
==============

Portable local + GitHub Codespaces manga/manhwa library.

LOCAL
-----

    ./run.sh

Then open:

    http://127.0.0.1:8765


GITHUB CODESPACES
-----------------

The repository includes .devcontainer/devcontainer.json.

When a Codespace is created:
- Python 3.12 dev container is used.
- requirements.txt is installed automatically.
- Port 8765 is forwarded automatically.
- The app starts automatically.

Open the forwarded "Manwa Manager" port from the Ports panel.


LIBRARY
-------

Settings and chosen library folder location are stored in:

    ~/.config/ManwaManager/config.json

Default library folder:

    ~/Manwa Library

You can change the library folder at any time from the Web UI.
The library is stored outside the application and outside the AppImage.


DOWNLOAD SAFETY / ORGANIZATION
------------------------------

- Each series gets its own folder.
- The original series URL is stored in .series.json.
- Downloading the same URL again resumes that series.
- Different series with the same display name are separated as:
      Series Name
      Series Name (2)
      Series Name (3)
- Existing valid pages are reused instead of downloaded again.

APPIMAGE
---------
The repository includes an AppImage build workflow (.github/workflows/build-appimage.yml).
Push the repository to GitHub and run the "Build AppImage" workflow. It produces a Linux x86_64 AppImage.

You can also build locally using:

    ./build-appimage.sh

The AppImage stores its settings in ~/.config/ManwaManager/config.json and allows choosing/changing a library folder from the UI. The library is not stored inside the AppImage.
