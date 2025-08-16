# Spleen

Spleen is a lightweight PyQt file manager built for Hyprland.

## Features

- Tabbed file browsing with live search filtering
- Context menu actions: open, rename, move to trash/delete, new folder, copy/move, properties and zip extraction
- Detailed properties dialog with permissions, ownership and timestamps
- Cut/Copy/Paste between directories
- Automatic detection of newly mounted drives
- Configurable default start directory saved across sessions
- Zoomable, resizable interface with persistent window size
- Watches directories for changes and refreshes automatically

## Installation

```bash
pip install PyQt5 watchdog
```

Run the application:

```bash
python spleen.py
```

Settings are stored automatically using Qt's `QSettings` and will be reused on next launch.

