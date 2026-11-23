# da2lm — technical notes

Players: see **GUIDE.md** in the project root.

Windows release: run `packaging\build_exe.bat` (Python 3.10+). It builds `dist\DA2 Loader\DA2 Loader.exe` with PyInstaller and zips it for upload.

Indexer, conflict checker and load-order manager for Dragon Age 2 mods. Pure Python 3.10+, no dependencies.
Scanning and reports are read-only. Only `lo set` / `lo move` with `--yes` rename folders, and `lo undo` reverses them.

## Desktop window

```
py -m pip install PySide6-Essentials      (once)
py -m da2lm.gui
```
It finds the game and override folders on its own (Steam libraries, EA app/Origin, classic folders, OneDrive Documents). If it can't, the banner asks you to pick them once; they're remembered. It checks your mods as soon as it opens and after every change. Rescans are quick: files whose size and date haven't changed aren't read again.

- **Banner** (top): *Ready to play*, *N things to check* or *Changes not applied yet*, with the one button that matters, plus **Play**.
- **Mods**: tick to use a mod, untick to set it aside (moved to `packages\core\da2lm_disabled`, never deleted). Drag or Move up/down to reorder; lower wins. **Apply changes** does everything in one step after showing what will happen. **Undo last reorder** puts folder names back.
- **Things to check**: each overlap in plain words, grouped by the mods involved: what happens, which mod's version the game uses, and what you can do (merge, change order, open character creation). *It's intended - mark OK* moves a clash out of the way; it stays listed under "Marked OK by you".
- **Character creation**: see below.
- **Details** and **Log**: the full technical report, for modders.

Readmes, screenshots, archives and add-in `manifest.xml` files are ignored: the game never loads them, so two mods shipping a readme isn't a clash.

Merged files da2lm made (tables, combined rows, character creation) are kept up to date automatically when your mods change; da2lm never creates one you didn't ask for.

## Run it (Windows)

```
cd da2lm
python -m da2lm scan                 # defaults to Documents\BioWare\Dragon Age 2\packages\core\override
python -m da2lm report               # real + unverified conflicts
python -m da2lm report --all         # also identical (harmless) duplicates
python -m da2lm report --json > conflicts.json
python -m da2lm inspect "path\to\some.erf" --limit 0
python -m da2lm order chargen.gda    # load order for one resource
```

Several roots, in load order (the later one wins):

```
python -m da2lm scan --root "C:\Games\Dragon Age 2\packages\core\override" ^
                     --root "%USERPROFILE%\Documents\BioWare\Dragon Age 2\packages\core\override"
```

`report` exits with code 2 if there's a real high-risk conflict, so it already works as a pre-launch check.

## Privacy

The tool never reads, copies or uploads `DragonAge.ini`: it holds an EA session token (`[AccountOptions]`). If a setting from it is ever needed, only that key will be read.

## Reporting principle

The tool reports every clash, as neutral facts: what overrides what, what changes, and who wins. It never assumes a clash is a mistake, because many are deliberate (an optional variant kept on purpose, a mod that must ship a whole table to delete rows). Flagging it is for the case where it *was* an oversight; deciding is up to you.

## How the game picks a winner (tested in game)

Confirmed in November 2026 with tinted main-menu textures:
1. **Loose files beat archives.** Any loose file in override wins over any copy inside an `.erf`/`.rim`, even when the archive is in a folder that loads later or has a name that sorts later.
2. **Inside each group, the later path wins**, compared the Windows way (uppercase, so `_` sorts after letters): `A_mod` loads after `AB_mod`.
3. **Folder structure doesn't matter.** A file in `override\anything\` is found by its name, so no `art\...` path is needed.

What this means for load order: renumbering folders with `lo` only decides between copies of the *same kind* (two loose files, or two archived copies). It can't make an archived file beat a loose one. To let an archive's version win, the loose copy has to be removed or renamed.

## How files are matched

DA2's own archives store most entries **without a name, only a hash** (over 320,000 of them in a full install). So the tool keys every resource by that hash: the FNV-1 64 hash of the lowercased file name, which is what the engine uses to look files up. A loose `ico_health_1.dds` in a mod, a hash-only copy inside another mod's `.erf` and the game's hash-only original all match. Names are shown when any copy carries one; otherwise the report shows `#<hash>`.

## Where DA2 keeps its tables

- **Base game:** `<DA2>\packages\core\data\2da.rim`. `.rim` files are the same archive format as `.erf`, and the tool reads both.
- **Each DLC:** `<DA2>\addins\<dlc>\core\data\2da.erf`. Pass `--vanilla "<DA2>\addins"` as well, so DLC tables count as game files and aren't flagged as mod changes.

## Compare against the game's files (recommended)

```
python -m da2lm scan --vanilla "C:\Program Files (x86)\Dragon Age 2\packages\core\data"
```

`--vanilla` reads only the *names* inside the game's ERFs, so the report can show:
- **Whole tables replaced:** a mod ships a game table under its exact name (e.g. `abi_base.gda`). The tool compares it with the game's copy and reports what it does: overrides values (CRITICAL, because other mods' edits to those rows lose), deletes rows (WARNING; a suffixed table can't delete, so this needs the whole file), or only adds rows / is identical (note). For DLC tables, replacing by name is the only method that works.
- **Other game files a mod overrides** (textures, scripts, levels…), counted, and listed with `--all`.

Without `--vanilla`, only a short built-in list of base tables is checked (`abi_base`, `apr_base`, `spec_base`, `exptable`, `itemprps`, `2da_base`). That list is a best guess, and the vanilla scan is the reliable option.

## Row overlaps in multi-part tables (`rows`, also part of `report`)

DA2 merges every table whose name starts with a known prefix into one (the game lists them in `m2da_base.gda`: `ABI_`, `EquipmentLayout`, `rewards`...). Two parts with the same row ID compete for that row, even with different file names. For every table a mod touches, the tool lines up all its parts and compares rows by ID:
- **identical values in every copy:** harmless, just counted (e.g. DFA and EYP Clean both setting companion slots to 16375).
- **different values:** a real conflict, with the predicted winner and how sure that is:
  - `confirmed`: a DLC row beats a mod's suffixed row (EYP / Tallis test).
  - `documented`: a mod's suffixed row beats the game's base row (how BioWare's own `abi_patch` works).
  - `unconfirmed`: two mod parts, or mod vs patch. Not tested in game yet.

A mod file with the same name as a game part replaces that part and takes its place (confirmed for DLC tables).

## Overrides inside one mod folder

`report` also lists files that override each other *inside* one mod's folder, from different files: for example optional variants kept next to the main files. Everything in override loads, so the later file is the active one.

## Merging mods that repack the same table

When several mods each ship a whole `abi_base.gda` (or any vanilla table), normally only the last one survives. `merge` makes them coexist:

```
python -m da2lm merge --vanilla "<DA2>\packages\core\data"          # preview
python -m da2lm merge --vanilla "<DA2>\packages\core\data" --yes    # write
python -m da2lm merge --clear                                          # undo
```

How it works:
1. Each mod's copy is compared with the vanilla table **cell by cell**, matching rows by their ID column.
2. Only the cells a mod actually changed, plus the rows it added, are applied, in load order.
3. A mod that left a cell at its vanilla value never overwrites another mod's change to that cell.
4. Two mods changing the **same cell** to different values is a real conflict. The later mod in load order wins, and the report lists it (row, column, both values).
5. A mod missing vanilla rows doesn't delete them (that's almost always an older repack). The rows are kept and reported.
6. A copy with different columns is skipped and reported.
7. The result goes into `override\~da2lm_merged\`, which sorts after every other folder, so its copy wins. The mods' own files are untouched. `lo` never renumbers that folder.
8. Re-run `merge --yes` after changing mods or load order. The folder is rebuilt each time.

Safety: before merging, it checks that the vanilla table survives a read/write round trip with nothing lost. If it doesn't, the table is not merged.

Check a table yourself:
```
python -m da2lm gda check "<DA2>\packages\core\data\2da.rim" --entry abi_base.gda
python -m da2lm gda dump  "<DA2>\packages\core\data\2da.rim" --entry abi_base.gda --rows 10
```
Column names aren't stored in GDAs, only CRC32 hashes, so most columns show as `col5#1a2b3c4d`.

Limits:
- If a mod was built on an older game version, its copy differs from your vanilla in rows it never meant to touch, and those differences get applied too. Pass the patch folder as a second `--vanilla` (`...\packages\core\patch`, if it exists) so the comparison uses the newest table.
- Suffixed M2DA files (`abi_base_mymod.gda`) are left alone. The game already merges those.

## Character creation (`chargenmorphcfg.xml`)

The game reads one `chargenmorphcfg.xml`: the heads, hairs, beards, colours and tattoos offered when you create Hawke. Many mods ship their own copy, and only one can win. da2lm merges them instead, so no separate merger tool is needed:

- Game entries come first, in the game's order; then each mod's new entries, in load order. Entries are matched by their attributes (name compared case-insensitively), so a mod that re-lists game entries doesn't duplicate them.
- Nothing is removed. If a mod's copy doesn't list some game entries (usually an older copy of the file), they stay in, and the tab says so.
- Untick a mod in the tab (or `da2lm chargen --exclude NAME`) to leave its additions out; the choice is remembered.
- The result is written to `~da2lm_merged\chargenmorphcfg.xml`, which loads last. Mod folders aren't touched. Rebuild after adding or removing a chargen mod; the tab shows when the file is out of date.
- Because it's handled here, `chargenmorphcfg.xml` isn't listed under Conflicts.

```
py -m da2lm chargen            preview what each mod adds
py -m da2lm chargen --yes      write the merged file
py -m da2lm chargen --clear    remove it
```

## Mods that edit the same rows of a multi-part table

Tables like abilities (`abi_*`), appearances (`apr_*`) or rewards are read as the game's base file plus every suffixed part. When two mods ship a part with the same row ID, the game takes the **whole row** from the one that loads later, so the other mod's edits to that row are lost, even if they changed different columns (one moves a talent in the tree, the other removes its level requirement).

**Merge them** (or `da2lm merge --yes`) builds one more part per table, `~da2lm_merged\<prefix>_da2lm_rows.gda`, with only those shared rows: the game's values, then each mod's changed cells in load order. Where two mods set the same cell differently, the later mod's value is used and it's listed. Rows a DLC also defines are left alone: DLC rows beat every mod part.

Combining columns is **opt-in per table** (`SAFE_COLUMNS` in `rowmerge.py`). Every other table keeps its rows whole: an appearance or item-variation row is one complete look, a rewards row is one designed list of items, so mixing two mods' columns would make something neither mod made. For those the window names the entries involved (e.g. the companions) and asks you to pick one (load order, or set a file aside).

Rewards are opt-in too: Copper, Silver, Gold and XP combine one by one, while the item list (item1–item6) always comes whole from one mod, so no reward ends up with a list nobody designed.

For abilities, and only for columns known to be independent settings: icon, tree position, prerequisites, level and points requirements, cost, cooldown. An ability's script and its effect values belong together: if two mods change anything else in the same ability differently, that row is kept whole too.

Which part wins between two mods: tested in game between loose files (later path wins, EYP vs Arsenal on rewards row 119). Between a loose part and one packed in an `.erf` it's not tested, and the window says "probable" for those.

**What each choice means**: for overlaps that can't be combined, the window lists every option (which mod loads last), what each mod keeps and what the others lose, and suggests one when a clear rule applies (a mod that copies the whole table with a broad tweak should load before mods that change specific entries; a known readme incompatibility settles it).

**Same mod installed twice**: two folders sharing most of their file names (an old folder left next to a renamed copy) are flagged, with the files whose versions differ.

## Merged files never go stale silently

After every build da2lm stores a fingerprint of everything the merged files came from (`~da2lm_merged\da2lm_sources.json`: every mod `.gda` and `chargenmorphcfg.xml`, by mod, file and content hash). Whenever the window checks your mods, and again right before **Play**, it compares: if a source changed, the merged files are rebuilt and the window says so. If they can't be rebuilt (game folder not set), it shows a warning instead. If you launch the game some other way after changing mods, open the window once first.

## Known incompatibilities

Some clashes only a mod's readme mentions. da2lm has a small list of them (`da2lm/known.py`), each with its source. First entry: Diversified Follower Armors' own `apr_base_ishdfa.gda` installed together with EYP. **Set the file aside** moves a loose file to `da2lm_disabled\_set aside files` (never deleted).

## Add-ins

Mods installed as add-ins (`Documents\BioWare\Dragon Age 2\addins\<name>`, with a `manifest.xml` that says `Type="AddIn"`) are managed like any other mod since 0.29:

- **Add mod from archive / folder** recognises an add-in folder and places it in `addins\` instead of override. An archive that ships both (an add-in folder plus override files, like Kirkwall Expanded) becomes two library mods: the add-in, and the override part.
- Add-ins already installed by hand show up in the list as "add-in installed by hand"; **Let da2lm manage them** takes them over like hand-installed override folders (same place, same files; Undo gives them back).
- Their place in the list doesn't matter (the game enables add-ins by manifest, not by order); they get no number.
- Turning an add-in off removes its folder; the plan warns that saves made with it won't load until it's back on.
- Clashes between an add-in and an override mod are still checked. How the game orders add-in content against override files is not tested yet.

## Mod library: order without renaming your folders

- Each mod lives once in da2lm's library (`%LOCALAPPDATA%\da2lm\library`). Turning it on places its files in `override\da2lm_mods\NNN_<name>\`; the number is the load order, and those folders are da2lm's, so reordering never renames yours.
- Files are placed as **hard links** when the library and override are on the same disk (a second name for the same data: no extra space, instant). On another disk, or where links aren't allowed, they're copied.
- A record (`%LOCALAPPDATA%\da2lm\deployments`) lists every file da2lm placed with its size and date. da2lm only removes or replaces files that still match it; a file changed by hand is left alone and reported.
- **Apply changes** takes over folders you installed by hand (into the library, as links, nothing deleted) and places everything in the chosen order. **Undo last change** puts the previous order back. **Add mod from archive / folder** brings new mods in (`.zip`; `.7z` and `.rar` through 7-Zip if it's installed, or `.7z` with `py7zr`).
- Library mods the record doesn't know (added by another tool, or by hand into the library) appear at the bottom of the list, off, marked "new in the library".
- **Turn all mods off** switches off every managed mod (press Apply; Undo brings them back): the game as it ships, for bug reports.
- After every scan, `%LOCALAPPDATA%\da2lm\inventory\<mod id>.json` lists each placed file and, for archives, every resource inside (name and size). Other tools can read what a mod ships without opening its files.
- Readmes and screenshots stay in the library; only game files reach override.
- Loose files directly in override are grouped by position and taken over as "Loose files".

## Leftover files in da2lm_mods

Every check lists files inside `da2lm_mods` that the current mod list doesn't account for: copies left
from an earlier order, files put there by hand, and mod files edited by hand (which da2lm otherwise
leaves alone forever). When such a file has the same name as a current mod file in an earlier folder,
it wins over it in game, so the entry goes to *Needs a look*. "Move them out" sets them aside in
`da2lm_disabled\_set aside files`; mod files then get their current version back on the next check.

## Load order (older method: renaming)

DA2 loads override alphabetically and the last copy wins, which is why mods get `z` / `zzz` prefixes.
`lo` replaces that with numeric prefixes: `010_Mod A`, `020_Mod B`, … Digits sort before letters, so the order is exact.

```
python -m da2lm lo list                                   # current order, last = wins
python -m da2lm lo move "Kirkwall Expanded" --bottom      # preview
python -m da2lm lo move "Kirkwall Expanded" --bottom --yes
python -m da2lm lo move "My Patch" --after "Kirkwall Expanded" --yes
python -m da2lm lo export > order.txt   # edit in any text editor: top loads first, bottom wins
python -m da2lm lo set order.txt --yes
python -m da2lm lo undo                 # reverses the last apply, can be repeated
```

- The original names are kept in `override\.da2lm\order.json`, and reports show the real mod names, not `020_…`.
- Every apply writes a journal there, which is what `undo` uses.
- A mod added later is *unmanaged*: it keeps its own name, so it loads after all numbered mods (it wins). `lo list` marks it, and the next `set`/`move` adds a number to it.
- Renames are two-phase (through temporary names), so swapping two mods can't collide.
- **Sorting detail:** Windows (NTFS) compares names in uppercase, where `_` sorts *after* letters, so `AB_mod` loads before `A_mod`. A lowercase comparison would give the opposite. The tool uses the NTFS order. Numeric prefixes sidestep the issue anyway.
- Loose files directly in `override` (outside any folder) can't be moved. They're listed separately.

## What it does

- Opens ERF V2.0 / V2.2 / V3.0, loose files too, and hashes the **unpacked** content (SHA-1).
- Uses the lowercased base name as the key, since the engine resolves by name, not by folder.
- Each conflict gets a verdict:
  - `real`: the copies have different content.
  - `identical`: the bytes are the same, so it's harmless.
  - `unverified`: some copy is encrypted or couldn't be decompressed.
- Each conflict also gets a risk level: high for `.ncs .are .cnv .mor .gda .plo .utc .uti .tlk` and chargen / char_stage / player_core names, medium for models and animations, low for the rest.
- Detects whether the V3.0 name hashes are FNV-1 or FNV-1a by checking the entries that have names. `scan` prints which one it found. Tell me what it says on real files.

## Known assumptions (verify, then fix the code)

1. **Which copy wins.** Alphabetical, last one wins: confirmed by how DA2 mods are used. Still to test: a loose file vs one inside an `.erf` in the same folder, and the `_` vs letters order described above (`A_test` and `AB_test` with the same visible texture).
2. **"Bioware zlib" compression.** Its framing isn't documented, so it tries the likely framings and keeps whichever produces the expected size. Anything else ends up `unverified`, never a false `identical`.
3. **Encrypted ERFs** (DLC) aren't decrypted. Their entries are still indexed by name, but their content is `unverified`.

## Not yet

Collisions between rows of suffixed M2DA files: two differently named `spec_base_*.gda` files that use the same row IDs.
The GDA reader now exists, so this is the next step. It is the check that matters for Arcane Warrior / Necromancer.

## Tests

`pip install pytest && python -m pytest -q`: 61 tests on synthetic data: ERF formats, conflicts, load-order apply/move/undo, GFF4/GDA round trips (V4.0 and V4.1), cell-level merge, the full merge pipeline, row overlaps, the character-creation merge and the scan cache.
