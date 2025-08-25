# NMS Newton (beta test version (0.2.1))

## Features:

 - Moving planets, in NMS!
 - Custom dialogue options at the Station Core interaction which lets you turn the planetary simulation off an on (mostly just a showcase/experiment).
 - Custom UI for showing the orbital period of the planet you are pointed at with your ship.

See a (not so great) video of it in action [here](https://youtu.be/ZrvR6W9D68o)

## Before you start:

This mod doesn't implement saving yet, so for now, always ensure that you start and end your session in your space ship or inside a spacce station otherwise you may get softlocked potentially (don't blame me if you mess up a save!)
This mod also does not handle moving POI's, or bases, or anything else really other than the planets.

## Installation:

1. Place the contents of this repo inside a folder under the usual `MODS` folder of No Man's Sky (ie. this file should be at `GAMEDATA/MODS/Newton/`)
2. Install a python version of at least 3.9. NOTE: Do not install from the windows store as this version will not work.
3. Ensure steam is running (Mod *may* work on GOG but is currently untested...).
4. Install [NMS.py](https://github.com/monkeyman192/NMS.py): `python -m pip install NMSpy`
5. Run NMS.py: `pymhf run nmspy`

If this is your first time using NMS.py, you will be prompted for a location for the mod folder. Specify your `MODS` folder.
You should not need to configure any other options, so you can just continue through and launch the game

The game should start up automatically and you should see a log window as well as another popup which lets you modify some parameters of the mod.
Once you have loaded in, you can untick the "Simulation paused" check box for the planets to start moving.
Note: They will be quite slow, so if you want them faster change the "Time rate". If you set this too high it may be impossible to land on planets, so the default speed while slow is meant to be a nice compromise (they move at a nice rate while you are on a planet - it's more noticable when they near the horizon!)

This mod can also be controlled by the in-game text chat.
Type `/mod newton` to see the options.
- `/mod newton enable` will turn on planetary motion.
- `/mod newton disable` will turn off planetary motion.
- `/mod newton speed X` (specify X as a number) will set the speed at which they move (1 being the "default rate").
