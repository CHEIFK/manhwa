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

All downloads stay inside:

    manwa_manager/manwa/

The path is relative to the application, so you can move the entire
manwa_manager folder anywhere.


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
The repository includes an AppImage build workflow. Push the repository to GitHub and run the "Build AppImage" workflow. It produces a Linux x86_64 AppImage.

The AppImage stores its settings in ~/.config/ManwaManager/config.json and asks the user to choose a library folder on first launch. The library is not stored inside the AppImage.
