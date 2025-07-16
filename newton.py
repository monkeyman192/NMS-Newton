# /// script
# dependencies = ["nmspy"]
# requires-python = ">=3.9, <=3.11"
#
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
# default_mod_save_dir = "."
#
# [tool.pymhf.gui]
# always_on_top = true
#
# [tool.pymhf.logging]
# log_dir = "."
# log_level = "info"
# window_name_override = "Newton"
# ///

import ctypes
from collections import namedtuple
from dataclasses import dataclass
import logging
import math
import random
import traceback
from typing import Optional

from pymhf import Mod, load_mod_file
from pymhf.core.memutils import get_addressof, map_struct
from pymhf.core.mod_loader import ModState
from pymhf.core.hooking import one_shot
from pymhf.gui import FLOAT, BOOLEAN
from pymhf.core.errors import NoSaveError

import nmspy.data.types as nms
import nmspy.data.exported_types as nmse
import nmspy.data.enums as enums
import nmspy.data.basic_types as basic


logger = logging.getLogger("Newton")


orbitParams = namedtuple("orbitParams", ["a", "b", "alpha", "delta"])


@dataclass
class NewtonState(ModState):
    """ Mod state which will be serialized for save data. """
    planet_times: list[float]
    stopped_planet_index: int
    fixed_planet_position: basic.Vector3f
    solar_system_center: basic.Vector3f
    fixed_center: basic.Vector3f
    is_in_orbit: bool = False


@dataclass
class SingletonState(ModState):
    """ Mod state which is just used for singletons which are to be retained across reloads. """
    planet_periods: list[str]
    orbit_params: list[Optional[orbitParams]]
    parent_planet_map: list[int]
    planet_indexes: set[int]
    moon_indexes: set[int]
    planet_seeds: list[int]
    planet_handles: list[Optional[basic.TkHandle]]
    planets: list[Optional[nms.cGcPlanet]]
    planets_moving: bool = False
    loaded_enough: bool = False
    grav_singleton: Optional[nms.cTkDynamicGravityControl] = None
    player_environment: Optional[nms.cGcPlayerEnvironment] = None
    orbital_period_buffers: list[ctypes.Array[ctypes.c_char]] = None
    GcApplication: Optional[nms.cGcApplication] = None


@dataclass
class NewtonGlobals:
    min_planet_epsilon: float
    max_planet_epsilon: float
    min_moon_epsilon: float
    max_moon_epsilon: float
    avg_planet_separation: float
    approach_rate_dropoff: int


def get_position_ellipse(center: basic.Vector3f, odata: orbitParams, t: float) -> basic.Vector3f:
    """ Generate the position at some given time based on the orbit parameters.
    NOTE: This will generate an orbit which has no variance in the z-direction.
    TODO: Have a normal vector arg or something to make orbits more interesting.
    """
    return basic.Vector3f(
        center.x + odata.a * math.cos(odata.alpha * t + odata.delta),
        center.y + odata.b * math.sin(odata.alpha * t + odata.delta),
        center.z
    )


# TODO:
# 0. Implement save/load system.
# 1. Cache planet environment properties when they are created to avoid looking
# up in memory every frame.
# 2. Change some state objects to be dicts instead of lists to make typing nicer
# and lookups easier.


class Newton(Mod):
    __author__ = "monkeyman192"
    __description__ = "Moving planets"
    __version__ = "0.2.0"

    save_state = NewtonState(
        planet_times=[0] * 8,
        stopped_planet_index=-1,
        fixed_planet_position=basic.Vector3f(0, 0, 0),
        solar_system_center=basic.Vector3f(0, 0, 0),
        fixed_center=basic.Vector3f(0, 0, 0),
    )

    state = SingletonState(
        parent_planet_map=[-1] * 8,
        planet_periods=[""] * 8,
        orbit_params=[None] * 8,
        planet_indexes=set(),
        moon_indexes=set(),
        planet_seeds=[0] * 8,
        planet_handles=[None] * 8,
        planets=[None] * 8,
        orbital_period_buffers=[None] * 8,
    )

    def __init__(self):
        super().__init__()
        self._time_rate = 1
        self.counter = 0
        self.lastRenderTimeMS = 0

        self.newton_globals = NewtonGlobals(
            min_planet_epsilon = 0.01,
            max_planet_epsilon = 0.2,
            min_moon_epsilon = 0,
            max_moon_epsilon = 0.05,
            avg_planet_separation = 300000.0,
            approach_rate_dropoff = 3,
        )

        # Create a string buffer once and then keep a fixed reference to it so
        # that we don't need to do it every frame.
        self.period_string_buffer = ctypes.c_char_p(b"PERIOD" + b"\x00" * 10)
        self._orbital_period_buffers = []

        self.run = True

        self._solarsystem_data: nmse.cGcSolarSystemData = None
        self._cached_hud_ptr = 0
        self._cached_hud: nms.cGcShipHUD = None
        self._cached_period_text_element: nms.cGcNGuiText = None

        self._solarsystem_data_ptr = 0

    @property
    @BOOLEAN("Simulation Running: ")
    def simulation_running(self):
        return self.state.planets_moving
    
    @simulation_running.setter
    def simulation_running(self, value):
        self.state.planets_moving = value

    @property
    @FLOAT("Time rate: ")
    def time_rate(self):
        return self._time_rate

    @time_rate.setter
    def time_rate(self, value):
        self._time_rate = value

    def update_gravity_center(self, index: int, new_position: basic.Vector3f):
        if self.state.grav_singleton is not None:
            center = self.state.grav_singleton.maGravityPoints[index].mCenter
            center.x = new_position.x
            center.y = new_position.y
            center.z = new_position.z

    def move_planet(self, index: int, new_position: basic.Vector3f):
        planet = self.state.planets[index]
        handle = self.state.planet_handles[index]
        if planet is not None and handle is not None:
            delta = new_position - planet.mPosition
            planet.mPosition = new_position
            planet.mRegionMap.mMatrix.pos = new_position
            nms.ShiftAllTransformsForNode(handle, delta)
            self.update_gravity_center(index, new_position)

    def generate_orbit_params(self, index: int, is_moon: bool):
        """ Generate the orbit parameters for the provided index. """
        random.seed(self.state.planet_seeds[index])
        delta = random.random() * math.tau
        epsilon = 0
        variance = 0.1 * self.newton_globals.avg_planet_separation

        # Determine the eccentricity of the orbit.
        if is_moon:
            epsilon = random.uniform(
                self.newton_globals.min_moon_epsilon,
                self.newton_globals.max_moon_epsilon
            )
        else:
            epsilon = random.uniform(
                self.newton_globals.min_planet_epsilon,
                self.newton_globals.max_planet_epsilon
            )

        # Determine the semi-minor axis first as we want this to be always clear
        # of the previous orbit.
        if is_moon:
            parent_planet = self.state.planets[self.state.parent_planet_map[index]]
            if parent_planet is not None:
                try:
                    parent_planet_radius = parent_planet.mRegionMap.mfCachedRadius
                    logger.debug(f"Parent planet radius: {parent_planet_radius}")
                    b = random.uniform(
                        1.75 * parent_planet_radius,
                        2.25 * parent_planet_radius
                    )
                except Exception:
                    logger.exception("There was an issue getting the planet radii")
                    b = (index + 1) * self.newton_globals.avg_planet_separation + variance * random.uniform(-1, 1)
            else:
                b = (index + 1) * self.newton_globals.avg_planet_separation + variance * random.uniform(-1, 1)
        else:
            b = (index + 1) * self.newton_globals.avg_planet_separation + variance * random.uniform(-1, 1)

        # Then calculate the semi-major axis from the eccentricity.
        a = b / math.sqrt(1 - epsilon * epsilon)

        # TODO: Make the mass a property of the planet based on its size.
        # TODO: Also do some maths to make the system more "caring" about the
        # mass of stuff. Eg add a mass for the sun and then base the periods off
        # this.
        # Do the same with planets.
        alpha = 3500000.0 / (math.tau * a ** 1.5)
        return orbitParams(a, b, alpha, delta)

    # This is stupid but one of these 3 will get it...

    @one_shot
    @nms.cTkDynamicGravityControl.Construct.after
    def load_gravity_singleton(self, this: ctypes._Pointer[nms.cTkDynamicGravityControl]):
        if self.state.grav_singleton is None:
            self.state.grav_singleton = this.contents

    @one_shot
    @nms.cTkDynamicGravityControl.cTkDynamicGravityControl.after
    def load_gravity_singleton2(self, this: ctypes._Pointer[nms.cTkDynamicGravityControl]):
        if self.state.grav_singleton is None:
            self.state.grav_singleton = this.contents

    @one_shot
    @nms.cTkDynamicGravityControl.GetGravity.after
    def load_gravity_singleton3(self, this: ctypes._Pointer[nms.cTkDynamicGravityControl], *args):
        if self.state.grav_singleton is None:
            self.state.grav_singleton = this.contents

    @nms.cGcApplicationLocalLoadState.GetRespawnReason.after
    def after_respawn(self, this, _result_):
        logger.debug(f"Starting to move the planets... Reason: {enums.RespawnReason(_result_).name}")
        self.state.loaded_enough = True

    def _format_planet_period(self, period: float) -> str:
        # Generate the string representation of the planet periods now so we can
        # just display them later
        # TODO: Just use HH:mm notation...
        suffix = "seconds"
        if 60 < period < 3600:
            suffix = "minutes"
            period = period / 60
        elif period >= 3600:
            suffix = "hours"
            period = period / 3600
        return f"{period:.2f} {suffix}"

    @nms.cGcPlanet.SetupRegionMap.after
    def after_planet_setup(self, this: ctypes._Pointer[nms.cGcPlanet]):
        planet = this.contents
        # Get some info about the planet and then store it so that we may access
        # it later.
        index = planet.miPlanetIndex
        self.state.planets[index] = planet
        self.state.planet_handles[index] = planet.mNode
        logger.debug(f"Planet is index {index} at position {planet.mPosition}")
        if self._solarsystem_data is not None:
            parent_planet_index = self._solarsystem_data.PlanetOrbits[index]
        else:
            parent_planet_index = -1

        self.state.parent_planet_map[index] = parent_planet_index

        if parent_planet_index == -1:
            is_moon = False
            self.state.planet_indexes.add(index)
        else:
            is_moon = True
            self.state.moon_indexes.add(index)

        self.state.planet_seeds[index] = planet.mPlanetGenerationInputData.Seed.Seed

        orb_params = self.generate_orbit_params(index, is_moon)
        self.state.orbit_params[index] = orb_params
        period = math.tau / orb_params.alpha

        self.state.planet_periods[index] = self._format_planet_period(period)
        self.state.orbital_period_buffers[index] = ctypes.create_string_buffer(
            f"Orbital Period: {self.state.planet_periods[index]}".encode()
        )

        if is_moon:
            parent_planet = self.state.planets[parent_planet_index]
            if parent_planet is not None:
                parent_planet_pos = parent_planet.mPosition
                pos = get_position_ellipse(
                    parent_planet_pos,
                    orb_params,
                    self.save_state.planet_times[index],
                )
                self.move_planet(index, pos)
        else:
            pos = get_position_ellipse(
                self.save_state.solar_system_center,
                orb_params,
                self.save_state.planet_times[index],
            )
            self.move_planet(index, pos)

    @nms.cGcShipHUD.RenderHeadsUp.before
    def before_render_HUD(self, this: ctypes._Pointer[nms.cGcShipHUD]):
        # Check to see if the offset has changed. If it has update the cached
        # value and then re-cache.
        if self._cached_hud is None:
            self._cached_hud = this.contents

            hud_root = self._cached_hud.mHeadsUpGUI.mRoot
            _text_layer = hud_root.FindTextRecursive(get_addressof(self.period_string_buffer))

            if _text_layer:
                self._cached_period_text_element = map_struct(_text_layer, nms.cGcNGuiText)

        if self._cached_hud is None or self._cached_period_text_element is None:
            return

        if not self._cached_hud.mbSelectedPlanetPanelVisible:
            # If the panel is not visible, then we don't need to do anything else.
            return

        text = self.state.orbital_period_buffers[self._cached_hud.miSelectedPlanet]
        # If the period is empty show nothing.
        # TODO: Disable the text field so nothing shows.
        if not text:
            return

        self._cached_period_text_element.mpTextData.contents.Text.set(text)

    def start_moving_planets(self):
        logger.debug("Planets starting to move...")
        self.state.planets_moving = True

    def stop_moving_planets(self):
        logger.debug("Stopping moving planets...")
        self.state.planets_moving = False

    @property
    def nearest_planet_index(self) -> int:
        # Return the index of the nearest planet
        if self.state.player_environment is not None:
            return self.state.player_environment.miNearestPlanetIndex
        return -1

    @one_shot
    @nms.cGcPlayerEnvironment.Update.after
    def get_player_env(self, this: ctypes._Pointer[nms.cGcPlayerEnvironment], lfTimeStep: float):
        self.state.player_environment = this.contents

    @nms.cGcSolarSystem.OnEnterPlanetOrbit.after
    def after_enter_orbit(self, *args):
        # When we enter the orbit, do a sanity check and then set the fixed
        # planet position.
        if self.state.planets_moving:
            if self.nearest_planet_index != -1:
                self.save_state.is_in_orbit = True
                nearest_planet = self.state.planets[self.nearest_planet_index]
                if nearest_planet is not None:
                    self.save_state.fixed_planet_position = nearest_planet.mPosition

    @nms.cTkStopwatch.GetDurationInSeconds.after
    def frame_time(self, this, _result_):
        # The main update loop doesn't have the delta time, so we'll get it here and then use it later.
        self.lastRenderTimeMS = _result_

    @nms.cGcSolarSystem.OnLeavePlanetOrbit.after
    def after_exit_orbit(self, this, lbAnnounceOSD):
        self.save_state.fixed_center = self.save_state.solar_system_center
        self.save_state.fixed_planet_position = basic.Vector3f(0, 0, 0)
        self.save_state.is_in_orbit = False

    def move_all_planets(self, delta: float):
        """ Move all the planets in the system. """
        nearest_planet_index = self.state.player_environment.miNearestPlanetIndex

        # If we are fully within the orbit of the nearest planet, then we will
        # not move it and everything else moves.
        if self.save_state.is_in_orbit:
            planet_to_not_move = nearest_planet_index
        else:
            # Otherwise, everything will be moving in some way.
            planet_to_not_move = -1

        # If the nearest "planet" is actually a moon, we also need to slow down
        # the parent planet.
        planet_to_slow = -1
        if nearest_planet_index in self.state.moon_indexes:
            planet_to_slow = self.state.parent_planet_map[nearest_planet_index]

        if planet_to_not_move == -1:
            # Move all planets (some may be slowed down).
            for idx in self.state.planet_indexes:
                # If the planet index is equal to the one which needs to be slowed
                # down due to getting close to its moon, do so now.
                if idx == planet_to_slow:
                    self.save_state.planet_times[idx] += self.time_modifier(nearest_planet_index) * delta
                else:
                    self.save_state.planet_times[idx] += self.time_modifier(idx) * delta
                new_pos = get_position_ellipse(
                    self.save_state.solar_system_center,
                    self.state.orbit_params[idx],
                    self.save_state.planet_times[idx],
                )
                self.move_planet(idx, new_pos)
            for idx in self.state.moon_indexes:
                self.save_state.planet_times[idx] += self.time_modifier(idx) * delta
                parent_planet = self.state.planets[self.state.parent_planet_map[idx]]
                new_pos = get_position_ellipse(
                    parent_planet.mPosition,
                    self.state.orbit_params[idx],
                    self.save_state.planet_times[idx],
                )
                self.move_planet(idx, new_pos)
        else:
            # To make the motion of other planets/moons look correct when on
            # another one, we need to move the center of the solar system in
            # the opposite motion to the motion of the body we are on.
            if planet_to_not_move in self.state.planet_indexes:
                expected_planet_pos = get_position_ellipse(
                    self.save_state.fixed_center,
                    self.state.orbit_params[planet_to_not_move],
                    self.save_state.planet_times[planet_to_not_move] + delta
                )
            else:
                # For a moon, move the solar system point as if it were the moon.
                # This will be the extected postion of the parent + the expected
                # position of the mooon.
                expected_parent_pos = get_position_ellipse(
                    self.save_state.fixed_center,
                    self.state.orbit_params[self.state.parent_planet_map[planet_to_not_move]],
                    self.save_state.planet_times[self.state.parent_planet_map[planet_to_not_move]] + delta,
                )
                expected_planet_pos = get_position_ellipse(
                    expected_parent_pos,
                    self.state.orbit_params[planet_to_not_move],
                    self.save_state.planet_times[planet_to_not_move] + delta
                )
            self.save_state.solar_system_center = self.save_state.fixed_center - expected_planet_pos + self.save_state.fixed_planet_position

            # Don't add the time modifier since we are on a planet (or close
            # enough it's not moving...)
            for idx in self.state.planet_indexes:
                self.save_state.planet_times[idx] += delta
                if idx != planet_to_not_move:
                    new_pos = get_position_ellipse(
                        self.save_state.solar_system_center,
                        self.state.orbit_params[idx],
                        self.save_state.planet_times[idx],
                    )
                    self.move_planet(idx, new_pos)
            for idx in self.state.moon_indexes:
                self.save_state.planet_times[idx] += delta
                if idx != planet_to_not_move:
                    parent_planet = self.state.planets[self.state.parent_planet_map[idx]]
                    new_pos = get_position_ellipse(
                        parent_planet.mPosition,
                        self.state.orbit_params[idx],
                        self.save_state.planet_times[idx],
                    )
                    self.move_planet(idx, new_pos)

    def time_modifier(self, index: int) -> float:
        """ Return a time modifier based on the planet index.
        This will be 1 for every planet except the nearest which will have a smooth drop off until 0.
        """
        pe = self.state.player_environment
        if index == pe.miNearestPlanetIndex:
            planet = self.state.planets[index]
            dist = pe.mfDistanceFromPlanet / 1000.0
            # Far point. Beyond this the rate will be 1
            far = 10 * planet.mpEnvProperties.contents.SkyAtmosphereHeight / 1000.0
            # Near point, at this point and closer the rate will be 0
            near = planet.mpEnvProperties.contents.AtmosphereEndHeight / 1000.0
            if near < dist < far:
                n = self.newton_globals.approach_rate_dropoff
                a_n = near ** n
                val = (dist ** n - a_n) / (far ** n - a_n)
                v = min(max(val, 0), 1)
                return v
            elif dist >= far:
                return 1
            elif dist < near:
                return 0
        return 1

    @one_shot
    @nms.cTkFSM.StateChange.after
    def tkfsm_state_change(self, this: ctypes._Pointer[nms.cTkFSM], *args):
        # TODO: Figure out best way to cast this...
        self.state.GcApplication = map_struct(this, nms.cGcApplication)

    @nms.cGcGameState.LoadFromPersistentStorage.after
    def load_data(self, this, a2, a3, lbNetworkClientLoad):
        # TODO: get the right save data.
        if self.state.GcApplication is not None:
            try:
                self.save_state.load(f"newton-{self.state.GcApplication.muPlayerSaveSlot}.json")
            except NoSaveError:
                pass
        else:
            pass

    @nms.cGcGameState.OnSaveProgressCompleted.after
    def after_save_data(self, *args):
        if self.state.GcApplication is not None:
            logger.info(f"Saved to slot {self.state.GcApplication.muPlayerSaveSlot}")
            self.save_state.save(f"newton-{self.state.GcApplication.muPlayerSaveSlot}.json")

    @nms.cGcApplication.Update.before
    def run_main_loop(self, this):
        if not self.run:
            return
        if self.state.GcApplication is not None:
            if self.state.GcApplication.mbPaused:
                # Don't move anything if the game is paused.
                return
        if self.state.loaded_enough and self.state.planets_moving:
            try:
                delta = self.time_rate * self.lastRenderTimeMS
                self.move_all_planets(delta)
            except Exception:
                logger.exception("Error moving the planets")
                self.run = False

    # Working for trying to figure out the moving textures on mineable asteroids...
    # @Engine.SetUniformArrayDefaultMultipleShaders.before
    # def shader_data(self, laShaderRes, liNumShaders, name, lpafData, liNumVectors):
    #     # TODO: If this has `name == gaPlanetPositionsVec4` then log it.
    #     _name = ctypes.c_char_p(name)
    #     if _name.value != b"gaPlanetPositionsVec4":
    #         return
    #     if self.counter < 50:
    #         shader_res = map_struct(laShaderRes, ctypes.c_int32)
    #         data = map_struct(lpafData, ctypes.c_float * (liNumVectors * 4))
    #         logger.info(f"uniforms: {_name.value} #shaders: {liNumShaders}, #vectors {liNumVectors}, res: {shader_res}")
    #         logger.info(f"Data: {list(data)}")
    #         self.counter += 1
    #     else:
    #         self._paused = True


if __name__ == "__main__":
    load_mod_file(__file__)
