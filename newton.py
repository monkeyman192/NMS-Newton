import ctypes
from collections import namedtuple
from dataclasses import dataclass
import logging
import math
import random
import time
import traceback
from typing import Annotated, Optional

from nmspy.calling import call_function
import nmspy.common as nms
import nmspy.data.engine as engine
import nmspy.data.function_hooks as hooks
from nmspy.memutils import map_struct, get_addressof, pprint_mem
from nmspy.data.common import Vector3f, TkHandle
import nmspy.data.structs as nms_structs
from nmspy.mod_loader import NMSMod, ModState
from nmspy.hooking import disable, main_loop, on_fully_booted, on_key_pressed


__disabled__ = True


newton_logger = logging.getLogger("Newton")


orbitParams = namedtuple("orbitParams", ["a", "b", "alpha", "delta"])


@dataclass
class NewtonState(ModState):
    planet_times: list[float]
    stopped_planet_index: int
    fixed_planet_position: Vector3f
    solar_system_center: Vector3f
    fixed_center: Vector3f
    planet_handles: list[Optional[TkHandle]]
    planets: list[Optional[nms_structs.cGcPlanet]]
    planet_periods: list[str]
    orbit_params: list[Optional[orbitParams]]
    parent_planet_map: list[int]
    planet_indexes: set[int]
    moon_indexes: set[int]
    planet_seeds: list[int]
    planets_moving: bool = False
    is_in_orbit: bool = False
    loaded_enough: bool = False


@dataclass
class NewtonGlobals:
    min_planet_epsilon: float
    max_planet_epsilon: float
    min_moon_epsilon: float
    max_moon_epsilon: float
    avg_planet_separation: float
    approach_rate_dropoff: int


def get_position_ellipse(center: Vector3f, odata: orbitParams, t: float) -> Vector3f:
    """ Generate the position at some given time based on the orbit parameters.
    NOTE: This will generate an orbit which has no variance in the z-direction.
    # TODO: Have a normal vector arg or something to make orbits more interesting.
    """
    return Vector3f(
        center.x + odata.a * math.cos(odata.alpha * t + odata.delta),
        center.y + odata.b * math.sin(odata.alpha * t + odata.delta),
        center.z
    )


def log_field_info(obj: ctypes.Structure, indent: int):
    base = ctypes.addressof(obj)
    prev_offset = 0
    prev_name = "Base"
    for field_name, field_type in obj._fields_:
        try:
            field = getattr(obj, field_name)
            if issubclass(field_type, ctypes.Structure):
                log_field_info(field, indent + 1)
            addr = ctypes.addressof(field) - base
            newton_logger.info("  " * indent + f"{field_name}: {addr} (0x{addr:X}) (+{addr - prev_offset} since {prev_name})")
            prev_offset = addr
            prev_name = field_name
        except:
            pass


# TODO:
# 0. Implement save/load system.
# 1. Cache planet environment properties when they are created to avoid looking
# up in memory every frame.
# 2. Change some state objects to be dicts instead of lists to make typing nicer
# and lookups easier.


class Newton(NMSMod):
    __author__ = "monkeyman192"
    __description__ = "Moving planets"
    __version__ = "0.1"

    state = NewtonState(
        planet_times=[0] * 8,
        stopped_planet_index=-1,
        fixed_planet_position=Vector3f(0, 0, 0),
        solar_system_center=Vector3f(0, 0, 0),
        fixed_center=Vector3f(0, 0, 0),
        planet_handles=[None] * 8,
        planets=[None] * 8,
        parent_planet_map=[-1] * 8,
        planet_periods=[""] * 8,
        orbit_params=[None] * 8,
        planet_indexes=set(),
        moon_indexes=set(),
        planet_seeds=[0] * 8,
    )

    def __init__(self):
        super().__init__()
        self.time_rate = 1
        self.switch = 0

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
        self.period_string_buffer_ptr = ctypes.c_char_p(b"PERIOD" + b"\x00" * 10)

        self.run = True

        self._solarsystem_data: Optional[nms_structs.cGcSolarSystemData] = None
        self._cached_hud_offset = 0
        self._cached_hud: Optional[nms_structs.cGcShipHUD] = None
        self._cached_period_text_element: Optional[nms_structs.cGcNGuiText] = None

    def update_gravity_center(self, index: int, new_position: Vector3f):
        if nms.gravity_singleton is not None:
            nms.gravity_singleton.gravityPoints[index].centre = new_position

    def move_planet(self, index: int, new_position: Vector3f):
        planet = self.state.planets[index]
        handle = self.state.planet_handles[index]
        if planet is not None and handle is not None:
            delta = new_position - planet.position
            planet.position = new_position
            planet.regionMap.matrix.pos = new_position
            engine.ShiftAllTransformsForNode(handle.lookupInt, ctypes.addressof(delta))
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
                    parent_planet_radius = parent_planet.regionMap.cachedRadius
                    newton_logger.info(f"Parent planet radius: {parent_planet_radius}")
                    b = random.uniform(
                        1.75 * parent_planet_radius,
                        2.25 * parent_planet_radius
                    )
                except:
                    newton_logger.exception(traceback.format_exc())
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

    @hooks.cGcSolarSystem.Generate.after
    def generate_solarsystem(self, this, *args):
        ss = map_struct(this, nms_structs.cGcSolarSystem)
        self._solarsystem_data = ss.solarSystemData
        # try:
        #     ssg = map_struct(this + 5364592, nms_structs.cGcSolarSystemGenerator)
        #     newton_logger.info(f"State 1: {ssg.RNG.state0}, State 2: {ssg.RNG.state1}")
        # except:
        #     newton_logger.exception(traceback.format_exc())


    @hooks.cGcApplicationLocalLoadState.GetRespawnReason.after
    def after_respawn(self, this, _result_):
        newton_logger.info(f"Starting to move the planets... Reason: {_result_}")
        self.state.planets_moving = True
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

    @hooks.cGcPlanet.SetupRegionMap.after
    def after_planet_setup(self, this):
        newton_logger.info(f"cGcPlanet*: {this}")
        planet = map_struct(this, nms_structs.cGcPlanet)
        # Get some info about the planet and then store it so that we may access
        # it later.
        index = planet.planetIndex
        self.state.planets[index] = planet
        self.state.planet_handles[index] = planet.node
        if self._solarsystem_data is not None:
            parent_planet_index = self._solarsystem_data.planetOrbits[index]
        else:
            parent_planet_index = -1

        self.state.parent_planet_map[index] = parent_planet_index

        if parent_planet_index == -1:
            is_moon = False
            self.state.planet_indexes.add(index)
        else:
            is_moon = True
            self.state.moon_indexes.add(index)

        self.state.planet_seeds[index] = planet.planetGenerationInputData.seed.Seed

        orb_params = self.generate_orbit_params(index, is_moon)
        self.state.orbit_params[index] = orb_params
        period = math.tau / orb_params.alpha

        self.state.planet_periods[index] = self._format_planet_period(period)

        if is_moon:
            parent_planet = self.state.planets[parent_planet_index]
            if parent_planet is not None:
                parent_planet_pos = parent_planet.position
                pos = get_position_ellipse(
                    parent_planet_pos,
                    orb_params,
                    self.state.planet_times[index],
                )
                self.move_planet(index, pos)
        else:
            pos = get_position_ellipse(
                self.state.solar_system_center,
                orb_params,
                self.state.planet_times[index],
            )
            self.move_planet(index, pos)

    @hooks.cGcShipHUD.RenderHeadsUp.before
    def before_render_HUD(self, this, *args):
        # Check to see if the offset has changed. If it has update the cached
        # value and then re-cache.
        if self._cached_hud_offset != this:
            self._cached_hud_offset = this
            self._cached_hud = map_struct(this, nms_structs.cGcShipHUD)
            hud_root = self._cached_hud.headsUpGUI.root
            _text_layer = call_function(
                "cGcNGuiLayer::FindTextRecursive",
                ctypes.addressof(hud_root),
                self.period_string_buffer_ptr,
            )

            if _text_layer:
                self._cached_period_text_element = map_struct(
                    _text_layer, nms_structs.cGcNGuiText
                )

        if self._cached_hud is None or self._cached_period_text_element is None:
            return

        if not self._cached_hud.selectedPlanetPanelVisible:
            # If the panel is not visible, then we don't need to do anything else.
            return

        selected_planet = self._cached_hud.selectedPlanet

        planet_period = self.state.planet_periods[selected_planet]
        # If the period is an empty string, just return.
        # TODO: Disable the text field so nothing shows.
        if planet_period == "":
            return

        text_data = self._cached_period_text_element.textData.contents.text
        text_data.set(f"Orbital Period: {planet_period}")

    @property
    def nearest_planet_index(self) -> int:
        # Return the index of the nearest planet
        return self.player_environment.miNearestPlanetIndex

    @property
    def player_environment(self) -> nms_structs.cGcPlayerEnvironment:
        return nms.GcApplication.data.contents.Simulation.environment.playerEnvironment

    @hooks.cGcSolarSystem.OnEnterPlanetOrbit.after
    def after_enter_orbit(self, *args):
        # When we enter the orbit, do a sanity check and then set the fixed
        # planet position.
        newton_logger.info(f"Entered orbit {self.nearest_planet_index}")
        if self.state.planets_moving:
            if self.nearest_planet_index != -1:
                self.state.is_in_orbit = True
                nearest_planet = self.state.planets[self.nearest_planet_index]
                if nearest_planet is not None:
                    self.state.fixed_planet_position = nearest_planet.position

    @hooks.cGcSolarSystem.OnLeavePlanetOrbit.after
    def after_exit_orbit(self, *args):
        newton_logger.info("Exited orbit")
        self.state.fixed_center = self.state.solar_system_center
        self.state.fixed_planet_position = Vector3f(0, 0, 0)
        self.state.is_in_orbit = False

    def move_all_planets(self, delta: float):
        """ Move all the planets in the system. """
        nearest_planet_index = self.player_environment.miNearestPlanetIndex

        # If we are fully within the orbit of the nearest planet, then we will
        # not move it and everything else moves.
        if self.state.is_in_orbit:
            planet_to_not_move = nearest_planet_index
        else:
            # Otherwise, everything will be moving in some way.
            planet_to_not_move = -1

        # If the nearest "planet" is actually a moon, we also need to slow down
        # the parent planet.
        planet_to_slow = -1
        if nearest_planet_index in self.state.moon_indexes:
            planet_to_slow = self.state.parent_planet_map[nearest_planet_index]
            # newton_logger.info(planet_to_slow)

        if planet_to_not_move == -1:
            # Move all planets (some may be slowed down).
            for idx in self.state.planet_indexes:
                # If the planet index is equal to the one which needs to be slowed
                # down due to getting close to its moon, do so now.
                if idx == planet_to_slow:
                    self.state.planet_times[idx] += self.time_modifier(nearest_planet_index) * delta
                else:
                    self.state.planet_times[idx] += self.time_modifier(idx) * delta
                new_pos = get_position_ellipse(
                    self.state.solar_system_center,
                    self.state.orbit_params[idx],
                    self.state.planet_times[idx],
                )
                self.move_planet(idx, new_pos)
            for idx in self.state.moon_indexes:
                self.state.planet_times[idx] += self.time_modifier(idx) * delta
                parent_planet = self.state.planets[self.state.parent_planet_map[idx]]
                new_pos = get_position_ellipse(
                    parent_planet.position,
                    self.state.orbit_params[idx],
                    self.state.planet_times[idx],
                )
                self.move_planet(idx, new_pos)
        else:
            # To make the motion of other planets/moons look correct when on
            # another one, we need to move the center of the solar system in
            # the opposite motion to the motion of the body we are on.
            if planet_to_not_move in self.state.planet_indexes:
                expected_planet_pos = get_position_ellipse(
                    self.state.fixed_center,
                    self.state.orbit_params[planet_to_not_move],
                    self.state.planet_times[planet_to_not_move] + delta
                )
            else:
                # For a moon, move the solar system point as if it were the moon.
                # This will be the extected postion of the parent + the expected
                # position of the mooon.
                expected_parent_pos = get_position_ellipse(
                    self.state.fixed_center,
                    self.state.orbit_params[self.state.parent_planet_map[planet_to_not_move]],
                    self.state.planet_times[self.state.parent_planet_map[planet_to_not_move]] + delta,
                )
                expected_planet_pos = get_position_ellipse(
                    expected_parent_pos,
                    self.state.orbit_params[planet_to_not_move],
                    self.state.planet_times[planet_to_not_move] + delta
                )
            self.state.solar_system_center = self.state.fixed_center - expected_planet_pos + self.state.fixed_planet_position

            # Don't add the time modifier since we are on a planet (or close
            # enough it's not moving...)
            for idx in self.state.planet_indexes:
                self.state.planet_times[idx] += delta
                if idx != planet_to_not_move:
                    new_pos = get_position_ellipse(
                        self.state.solar_system_center,
                        self.state.orbit_params[idx],
                        self.state.planet_times[idx],
                    )
                    self.move_planet(idx, new_pos)
            for idx in self.state.moon_indexes:
                self.state.planet_times[idx] += delta
                if idx != planet_to_not_move:
                    parent_planet = self.state.planets[self.state.parent_planet_map[idx]]
                    new_pos = get_position_ellipse(
                        parent_planet.position,
                        self.state.orbit_params[idx],
                        self.state.planet_times[idx],
                    )
                    self.move_planet(idx, new_pos)

    def time_modifier(self, index: int) -> float:
        pe = self.player_environment
        if index == pe.miNearestPlanetIndex:
            planet = self.state.planets[index]
            dist = pe.mfDistanceFromPlanet / 1000.0
            # Far point. Beyond this the rate will be 1
            far = 10 * planet.envProperties.contents.skyAtmosphereHeight / 1000.0
            # Near point, at this point and closer the rate will be 0
            near = planet.envProperties.contents.atmosphereEndHeight / 1000.0
            if near < dist < far:
                n = self.newton_globals.approach_rate_dropoff
                a_n = near ** n
                val = (dist ** n - a_n) / (far ** n - a_n)
                v = min(max(val, 0), 1)
                # newton_logger.info(f"index: {index}, val: {val}, v: {v}")
                return v
            elif dist >= far:
                return 1
            elif dist < near:
                return 0
        return 1

    @main_loop.before
    def run_main_loop(self):
        if not self.run:
            return
        if self.state.loaded_enough and not nms.GcApplication.paused:
            try:
                delta = self.time_rate * nms.GcApplication.lastRenderTimeMS / 1000.0
                self.move_all_planets(delta)
            except:
                newton_logger.exception(traceback.format_exc())
                self.run = False
            # sim = nms.GcApplication.data.contents.Simulation
            # env = sim.environment.playerEnvironment
            # try:
            #     newton_logger.info(f"Player transform matrix: {str(env.mPlayerTM)}")
            # except Exception as e:
            #     newton_logger.exception(e)
            # newton_logger.info(f"Player is inside planet atmosphere? {env.mbInsidePlanetAtmosphere}")

    # @on_fully_booted
    # def checks(self):
    #     data = nms.GcApplication.data.contents
    #     base_addr = ctypes.addressof(data)
    #     newton_logger.info(f"Base: 0x{base_addr:X}")
    #     sim_addr = ctypes.addressof(data.Simulation)
    #     newton_logger.info(f"Simulation: 0x{sim_addr - base_addr:X} == 0x443F20 ???")
    #     env = data.Simulation.environment.playerEnvironment
    #     newton_logger.info(f"playerEnvironment: 0x{ctypes.addressof(env) - sim_addr:X} == 0x99ec0 ???")
    #     mbInsidePlanetAtmosphere = env.mbInsidePlanetAtmosphere
    #     newton_logger.info(f"mbInsidePlanetAtmosphere: 0x{ctypes.addressof(mbInsidePlanetAtmosphere) - ctypes.addressof(env):X} == 0x8C")
