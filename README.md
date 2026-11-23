# da2lm: Dragon Age II mod loader

A mod loader for **Dragon Age II** on Windows. It keeps your mods in a library, places them in the
game's override folder in the order you choose, and tells you in plain words when two mods change
the same thing, and which one the game actually uses.

> **Beta.** Back up `Documents\BioWare\Dragon Age 2\packages\core\override` before the first run.

## What it does

- **Load order without renaming folders.** Switch mods on and off and drag them into order;
  lower in the list wins.
- **Takes over mods you installed by hand** without changing which mod wins anything, and gives
  them back with Undo.
- **Finds real conflicts, not just duplicate file names.** It reads inside `.erf` packages and
  compares game tables (`.gda`) row by row, so it can tell "these two mods both edit Isabela's skill
  trees" from "these two mods ship the same texture".
- **Merges what can be merged.** Mods that each ship a full copy of the same table, or edit
  different rows of it, are combined into one file, so every mod's changes apply. Character
  creation options from several mods (`chargenmorphcfg.xml`) are combined too.
- **Flags leftovers.** Old copies or hand-edited files inside its own folders that would quietly
  override a newer mod file.
- **Add-ins supported.** Mods that belong in `Documents\BioWare\Dragon Age 2\addins` are placed
  there automatically.
- **Undo** for every change, and **Turn all mods off** to run the game clean.

## Download

Get `da2lm-<version>-win64.zip` from [Releases](../../releases) (or from the Nexus Mods page), unpack it anywhere (not inside the
game folder) and run **DA2 Loader.exe**. Nothing else to install.

Windows may say the app is from an unknown publisher, because it isn't code-signed: choose
*More info → Run anyway*. Some antivirus programs flag apps packaged with PyInstaller even when
they are clean; every release is built from this repository by GitHub Actions (see below), and its
SHA-256 is published next to the zip so you can check the file you have.

See **[GUIDE.md](GUIDE.md)** for a short walkthrough.

## What it touches on your PC

| Where | What |
|---|---|
| `Documents\BioWare\Dragon Age 2\packages\core\override` | `da2lm_mods\` (the mods it places) and `~da2lm_merged\` (merged tables) |
| `Documents\BioWare\Dragon Age 2\addins` | add-in mods you choose to install |
| `Documents\BioWare\Dragon Age 2\packages\core\da2lm_disabled` | files it sets aside (never deletes) |
| `%LOCALAPPDATA%\da2lm` | its library of mods, the record of what it placed, a scan cache |
| `%APPDATA%\da2lm\config.json` | the folders you picked and your choices |

It does **not** modify the game's own files, needs no administrator rights, and makes **no
network connections**: no updates, telemetry or downloads. **Play** starts the game's own
executable.

Files are placed as hard links when possible, so mods don't take disk space twice.

## Run from source

Python 3.10 or newer:

```
py -m pip install PySide6-Essentials
```

then double-click `Open DA2 Loader.pyw`. The command line (`py -m da2lm --help`) offers the same
checks as text reports.

## Build the .exe yourself

```
packaging\build_exe.bat
```

It installs PyInstaller and PySide6, builds `dist\DA2 Loader\DA2 Loader.exe` and zips it.
Releases on this page are built the same way on GitHub's Windows machines.

## Tests

```
py -m pip install pytest
py -m pytest tests
```

## Reporting problems

Open an issue with the contents of the **Log** tab, what you did, where the game is installed
(Steam, EA App/Origin, other) and whether your Documents folder is inside OneDrive.

Technical notes on how conflicts are detected and merged: [docs/TECHNICAL.md](docs/TECHNICAL.md).

## Credits

- Made by Nacho, as part of DA2 Reforged.
- [Cinzel](https://github.com/NDISCOVER/Cinzel) font, © The Cinzel Project Authors, SIL Open Font License 1.1
  (`da2lm/assets/Cinzel-OFL.txt`).
- Dragon Age is a trademark of Electronic Arts. This is an unofficial fan tool, not affiliated with
  EA or BioWare.

## License

All rights reserved: you may download, read, build and use it for personal use; redistributing it,
publishing modified versions or reusing its code needs the author's permission. See
[LICENSE](LICENSE).
