# da2lm: quick guide

da2lm is a mod loader for Dragon Age II. It keeps your mods in its own library, places them in the
game's override folder in the order you choose, and tells you in plain words when two mods change
the same thing.

**This is a beta.** Before the first run, back up
`Documents\BioWare\Dragon Age 2\packages\core\override` (copy the whole folder somewhere safe).

## 1. First start

1. Unpack the download anywhere you like (not inside the game folder) and double-click
   **DA2 Loader.exe**. Nothing else to install.
   Windows may warn that the app is from an unknown publisher (it isn't signed): choose
   *More info → Run anyway*.
   *Running from source instead:* install [Python](https://www.python.org/downloads/) 3.10+, run
   `py -m pip install PySide6-Essentials`, then double-click `Open DA2 Loader.pyw`.
2. da2lm looks for the game and for your override folder. If it can't find them, use
   **Game and mod folders...** at the bottom of the window.
3. It checks your mods. The first check can take a few minutes with large texture packs; later
   checks are quick.

Nothing in your override folder is changed by the first check.

## 2. Mods you already installed by hand

If you installed mods before (folders inside `override`), the Home tab says how many.
**Let da2lm manage them** moves them into da2lm's library **without changing which mod wins
anything**. After that you can switch them on and off and reorder them. **Undo** gives
them back exactly as they were.

You don't have to do this: da2lm also works next to hand-installed mods. It just can't reorder those.

## 3. Adding a mod

- **Add a mod → Archive...**: a .zip, .7z or .rar as downloaded from Nexus (.7z and .rar need
  7-Zip installed).
- **Add a mod → Folder...**: a folder you already unpacked.

New mods arrive switched **off**. Switch them on, put them where you want, then **Apply changes**.

Add-ins (mods that go to `Documents\BioWare\Dragon Age 2\addins`, with a `Manifest.xml`) are
recognised and placed there automatically.

## 4. Order: who wins

In the **Mods** tab, **lower in the list wins** when two mods change the same thing. Drag a mod or
use **Move up / Move down**, then **Apply changes**.

## 5. Things to check

This tab lists what needs a decision, in plain language:

- **Needs a look**: two mods change the same thing and one of them loses something. Each entry says
  what happens now and what you can do (merge them, change the order, keep one).
- **For your information**: overlaps that da2lm already solved, for example two mods' tables merged
  into one.
- **Marked OK by you**: anything you decided is fine as it is.
- **Leftover files in da2lm's mod folders**: files that no mod in your list accounts for (old copies,
  files dropped there by hand, mod files edited by hand). If they hide a newer mod file, this is
  listed under *Needs a look*. **Move them out** puts them in `packages\core\da2lm_disabled\_set aside files`;
  nothing is deleted.

## 6. Playing

Press **Play** when the banner says *Ready to play*. Don't change mods while the game is running:
close the game first.

## 7. Going back

- **Undo** undoes the last thing da2lm did (apply, adopt, update, remove).
- **Turn all mods off** switches every mod off, so the game runs without them. Your mods stay in the
  library; switch them back on any time.
- A removed mod can be brought back with **Undo**. Once the removal is no longer
  undoable, da2lm clears it from its library.

## Where things are

| What | Where |
|---|---|
| Your mods (library) | `%LOCALAPPDATA%\da2lm\library` |
| What da2lm placed | `override\da2lm_mods\<number>_<mod name>` |
| Merged tables | `override\~da2lm_merged` |
| Files set aside | `packages\core\da2lm_disabled` |

da2lm places files as hard links when it can, so they take no extra disk space.

## Reporting a problem

Open the **Log** tab, copy its contents, and include it with a short description of what you did.
Also say where your game is installed (Steam, EA App/Origin, other) and whether your Documents
folder is inside OneDrive.
