# /// script
# dependencies = ["nmspy"]
# requires-python = ">=3.9, <=3.11"
# 
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
#
# [tool.pymhf.gui]
# always_on_top = true
# shown = true
# 
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "Interactions"
# ///

import ctypes
from logging import getLogger

from pymhf import Mod, load_mod_file
from pymhf.core.memutils import map_struct
from pymhf.core.mod_loader import mod_manager
from pymhf.gui.decorators import no_gui

import nmspy.data.types as nms
from nmspy.data.basic_types import cTkFixedString

from newton import Newton

logger = getLogger()


@no_gui
class InteractionMod(Mod):
    __dependencies__ = ["Newton"]

    def __init__(self):
        super().__init__()
        self._seen_puzzles = set()

    @property
    def newton_is_enabled(self):
        return mod_manager[Newton].state.planets_moving

    @nms.cGcRewardManager.GiveGenericReward.before
    def give_generic_reward(
        self,
        this,
        lRewardID: ctypes._Pointer[cTkFixedString[0x10]],
        lMissionID,
        lSeed,
        lbPeek: bool,
        lbForceShowMessage: bool,
        liOutMultiProductCount,
        lbForceSilent: bool,
        *args
    ):
        # This is called 4 times every time you receive something for *some reason*.
        # To avoid any side effects, we'll only do something if some of the parameters match an arbitrary set.
        reward_id = lRewardID.contents
        if (lbPeek, lbForceShowMessage, lbForceSilent) == (False, False, False):
            if reward_id == "MOD_NEWTON_SWAP":
                if not self.newton_is_enabled:
                    mod_manager[Newton].start_moving_planets()
                else:
                    mod_manager[Newton].stop_moving_planets()
                self._seen_puzzles.remove("STATION_CORE")
            logger.debug(f"Giving generic reward: {reward_id}, {args}")

    @nms.cGcInteractionComponent.GetPuzzle.after
    def get_puzzle(self, this: ctypes._Pointer[nms.cGcInteractionComponent], _result_):
        # Determine which puzzle to get. We use this to allow the puzzle to switch depending on whether
        # Newton is enabled or not.
        result = map_struct(_result_, nms.cGcAlienPuzzleEntry)
        if str(result.Id) in self._seen_puzzles:
            return
        if result.Id == "STATION_CORE":
            for option in result.Options:
                if self.newton_is_enabled and option.Name == "MOD_NEWTON_CORE_ACTIVATE":
                    option.Name.set("MOD_NEWTON_CORE_DEACTIVATE")
                elif not self.newton_is_enabled and option.Name == "MOD_NEWTON_CORE_DEACTIVATE":
                    option.Name.set("MOD_NEWTON_CORE_ACTIVATE")
        self._seen_puzzles.add(str(result.Id))


if __name__ == "__main__":
    load_mod_file(__file__)
