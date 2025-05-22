# /// script
# dependencies = ["pymhf[gui]==0.1.12.dev3"]
# requires-python = ">=3.9, <=3.11"
#
# [tool.uv.sources]
# pymhf = { index = "pypi_test" }
#
# [[tool.uv.index]]
# name = "pypi_test"
# url = "https://test.pypi.org/simple/"
# explicit = true
# 
# [tool.pymhf]
# exe = "NMS.exe"
# steam_gameid = 275850
# start_paused = false
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
from enum import IntEnum
import logging
import math
import random
import traceback
import types
from typing import Annotated, Any, Generator, Optional, Union, TypeVar, Generic, Type

from pymhf.core.memutils import map_struct

from pymhf.core._internal import BASE_ADDRESS
from pymhf import Mod, load_mod_file
from pymhf.core.memutils import get_addressof
from pymhf.core.mod_loader import ModState
from pymhf.core.hooking import one_shot, static_function_hook
from pymhf.gui import FLOAT, BOOLEAN
from pymhf.core.hooking import function_hook, Structure
from pymhf.utils.partial_struct import partial_struct, Field


# NMS basic types

CTYPES = Union[ctypes._SimpleCData, ctypes.Structure, ctypes._Pointer]

T = TypeVar("T", bound=CTYPES)
N = TypeVar("N", bound=int)


class Vector3f(ctypes.Structure):
    x: float
    y: float
    z: float

    _fields_ = [
        ("x", ctypes.c_float),
        ("y", ctypes.c_float),
        ("z", ctypes.c_float),
        ("_padding", ctypes.c_byte * 0x4),
    ]

    def __iadd__(self, other: "Vector3f"):
        self.x += other.x
        self.y += other.y
        self.z += other.z
        return self

    def __add__(self, other: "Vector3f"):
        return Vector3f(self.x + other.x, self.y + other.y, self.z + other.z)

    def __sub__(self, other: "Vector3f"):
        return Vector3f(self.x - other.x, self.y - other.y, self.z - other.z)

    def __mul__(self, other: Union[float, int]):
        if isinstance(other, Vector3f):
            raise NotImplementedError("To multiply two vectors, use a @ b to compute the dot product")
        return Vector3f(other * self.x, other * self.y, other * self.z)

    def __rmul__(self, other: Union[float, int]):
        return self * other

    def __matmul__(self, other: "Vector3f") -> float:
        """ Dot product """
        return self.x * other.x + self.y * other.y + self.z * other.z

    def __neg__(self):
        return Vector3f(-self.x, -self.y, -self.z)

    def __repr__(self):
        return f"Vector3f({self.x}, {self.y}, {self.z})"

    def __str__(self) -> str:
        return f"<{self.x, self.y, self.z}>"

    def __json__(self) -> dict:
        return {"x": self.x, "y": self.y, "z": self.z}

    def normalise(self) -> "Vector3f":
        """ Return a normalised version of the vector. """
        return ((self.x ** 2 + self.y ** 2 + self.z ** 2) ** (-0.5)) * Vector3f(self.x, self.y, self.z)

    def __len__(self) -> float:
        return (self.x ** 2 + self.y ** 2 + self.z ** 2) ** (0.5)


class GcSeed(ctypes.Structure):
    Seed: int
    UseSeedValue: bool
    _fields_ = [
        ("Seed", ctypes.c_longlong),
        ("UseSeedValue", ctypes.c_ubyte),
        ("padding0x9", ctypes.c_ubyte * 0x7)
    ]


class cTkFixedString(ctypes.Structure):
    _size: int
    value: bytes

    def set(self, val: str):
        self.value = val[:self._size].encode()

    def __class_getitem__(cls: type["cTkFixedString"], key: int):
        _cls: type["cTkFixedString"] = types.new_class(f"cTkFixedString<0x{key:X}>", (cls,))
        _cls._size = key
        _cls._fields_ = [
            ("value", ctypes.c_char * key)
        ]
        return _cls

    def __str__(self) -> str:
        return self.value.decode()

    def __repr__(self) -> str:
        return str(self)


class TkHandle(ctypes.Union):
    class TkHandleSub(ctypes.Structure):
        _fields_ = [
            ("lookup", ctypes.c_uint32, 18),
            ("incrementor", ctypes.c_uint32, 14),
        ]
    _anonymous_ = ("_sub",)
    _fields_ = [
        ("_sub", TkHandleSub),
        ("lookupInt", ctypes.c_uint32)
    ]
    lookupInt: int


class cTkMatrix34(ctypes.Structure):
    right: Vector3f
    up: Vector3f
    at: Vector3f
    pos: Vector3f

    _fields_ = [
        ("right", Vector3f),
        ("up", Vector3f),
        ("at", Vector3f),
        ("pos", Vector3f),
    ]

    @property
    def matrix(self):
        return (
            (self.right.x, self.right.y, self.right.z, 0),
            (self.up.x, self.up.y, self.up.z, 0),
            (self.at.x, self.at.y, self.at.z, 0),
            (self.pos.x, self.pos.y, self.pos.z, 1),
        )

    def __str__(self) -> str:
        return f"<right: {str(self.right)}, up: {str(self.up)}, at: {str(self.at)}, pos: {str(self.pos)}>"


class cTkAABB(ctypes.Structure):
    min: Vector3f
    max: Vector3f
    _fields_ = [
        ("min", Vector3f),
        ("max", Vector3f),
    ]


class cTkClassPool(ctypes.Structure, Generic[T, N]):
    _size: int
    _template_type: T
    pool: list[T]
    uniqueIds: list[int]
    roster: list[int]
    rosterPartition: int
    uniqueIDGenerator: int

    def __class_getitem__(cls: Type["cTkClassPool"], key: tuple[Type[T], int]):
        _type, _size = key
        _cls: Type[cTkClassPool[T, N]] = types.new_class(
            f"cTkClassPool<{_type}, {_size}>", (cls,)
        )
        _cls._fields_ = [  # type: ignore
            ("pool", _type * _size),
            ("uniqueIds", ctypes.c_int32 * _size),
            ("roster", ctypes.c_int32 * _size),
            ("rosterPartition", ctypes.c_int32),
            ("uniqueIDGenerator", ctypes.c_int32),
        ]
        return _cls


class cTkDynamicArray(ctypes.Structure, Generic[T]):
    _template_type: T
    _fields_ = [
        ("Array", ctypes.c_uint64),
        ("Size", ctypes.c_uint32),
        ("AllocatedFromData", ctypes.c_ubyte),
        ("_magicPad", ctypes.c_char * 0x3)
    ]

    Array: int
    Size: int
    allocatedFromData: bool

    @property
    def value(self) -> ctypes.Array[T]:
        if self.Array == 0 or self.Size == 0:
            # Empty lists are store with an empty pointer in mem.
            return []
        return map_struct(self.Array, self._template_type * self.Size)

    def set(self, data: ctypes.Array[T]):
        self.Array = get_addressof(data)
        self.Size = len(data) + 1

    def __iter__(self) -> Generator[T, None, None]:
        # TODO: Improve to generate as we go.
        for obj in self.value:
            yield obj

    def __getitem__(self, i: int) -> T:
        return self.value[i]

    def __repr__(self) -> str:
        return f"{self.__class__.__name__}({self.Size})"

    def __class_getitem__(cls: type["cTkDynamicArray"], key: Union[tuple[T], Any]):
        _cls: type["cTkDynamicArray"] = types.new_class(f"cTkDynamicArray<{key}>", (cls,))
        _cls._template_type = key
        return _cls

    def __len__(self) -> int:
        return self.Size


# NMS Data structures


@partial_struct
class cGcSolarSystemData(Structure):
    PlanetPositions: Annotated[list[Vector3f], Field(Vector3f * 8, 0x1DE0)]
    PlanetOrbits: Annotated[list[int], Field(ctypes.c_int32 * 8, 0x21D0)]
    Planets: Annotated[int, Field(ctypes.c_int32, 0x2264)]


@partial_struct
class cGcNGuiTextData(Structure):
    Text: Annotated[cTkDynamicArray[ctypes.c_char], Field(cTkDynamicArray[ctypes.c_char], 0x88)]


@partial_struct
class cGcNGuiText(Structure):
    mpTextData: Annotated[ctypes._Pointer[cGcNGuiTextData], Field(ctypes._Pointer[cGcNGuiTextData], 0x180)]

    @function_hook("40 53 48 83 EC ? 48 8B 99 ? ? ? ? 48 81 C3")
    def SetText(
        self,
        this: ctypes.c_uint64,
        lpacText: ctypes.c_uint64,
        # this: ctypes._Pointer["cGcNGuiText"],
        # lpacText: ctypes._Pointer[cTkFixedString[512]],
    ):
        pass


@partial_struct
class cGcNGuiLayer(Structure):
    @function_hook("48 83 EC ? 4C 8B 02 4C 8B C9 0F 10 02 49 8B C0 48 B9 ? ? ? ? ? ? ? ? 48 33 42 ? 48 0F AF C1 0F 11 44 24 ? 48 8B D0 48 C1 EA ? 48 33 D0 49 33 D0 48 0F AF D1 4C 8B C2 49 C1 E8 ? 4C 33 C2 4C 0F AF C1 41 8B C8 41 0F B7 D0 81 C2 ? ? ? ? C1 E9 ? 8B C2 49 C1 E8 ? C1 E0 ? 81 E1 ? ? ? ? 33 C8 33 D1 41 0F B7 C8 8B C2 41 C1 E8 ? C1 E8 ? 41 81 E0 ? ? ? ? 03 D0 03 D1 8B C2 C1 E0 ? 44 33 C0 41 33 D0 41 B8 ? ? ? ? 8B C2 C1 E8 ? 03 D0 8D 04 D5 ? ? ? ? 33 D0 8B C2 C1 E8 ? 03 D0 8B C2 C1 E0 ? 33 D0 8B C2 C1 E8 ? 03 D0 8B C2 C1 E0 ? 33 D0 8B C2 C1 E8 ? 03 C2 48 8D 54 24 ? 69 C0 ? ? ? ? C1 C8 ? 69 C8 ? ? ? ? 83 F1 ? C1 C9 ? 8D 0C 89 81 C1 ? ? ? ? 48 89 4C 24 ? 49 8B C9 E8 ? ? ? ? 48 83 C4 ? C3 CC CC CC CC CC CC 0F B6 41")
    def FindTextRecursive(
        self,
        # this: ctypes._Pointer["cGcNGuiLayer"],
        this: ctypes.c_uint64,
        lID: ctypes.c_uint64,
    ) -> ctypes.c_uint64:  # cGcNGuiElement *
        pass

    @function_hook("40 55 57 41 57 48 83 EC ? 4C 8B 89")
    def FindElementRecursive(
        self,
        # this: ctypes._Pointer["cGcNGuiLayer"],
        this: ctypes.c_uint64,
        lID: ctypes.c_uint64,  # const cTkHashedNGuiElement *
        leType: ctypes.c_uint32,  # eNGuiGameElementType
    ) -> ctypes.c_uint64:  # cGcNGuiElement *
        pass


@partial_struct
class cGcNGui(Structure):
    mRoot: Annotated[cGcNGuiLayer, Field(cGcNGuiLayer)]


@partial_struct
class cGcShipHUD(Structure):
    # The following offset is found from cGcShipHUD::RenderHeadsUp below the 2nd
    # cGcNGuiLayer::FindElementRecursive call.
    miSelectedPlanet: Annotated[int, Field(ctypes.c_uint32, 0x23BF0)]
    mbSelectedPlanetPanelVisible: Annotated[bool, Field(ctypes.c_bool, 0x23C00)]

    # The following offset is found by searching for "UI\\HUD\\SHIP\\MAINSCREEN.MXML"
    # (It's above the below entry.)
    mMainScreenGUI: Annotated[cGcNGui, Field(cGcNGui, offset=0x275D8)]
    # The following offset is found by searching for "UI\\HUD\\SHIP\\HEADSUP.MXML"
    mHeadsUpGUI: Annotated[cGcNGui, Field(cGcNGui, offset=0x27B90)]

    # hud_root: Annotated[int, Field(ctypes.c_ulonglong, 0x27F70)]  # TODO: Fix

    @function_hook("48 89 5C 24 ? 57 41 54 41 55 41 56 41 57 48 81 EC")
    def LoadData(self, this: ctypes._Pointer["cGcShipHUD"]):
        pass

    @function_hook("40 55 53 41 57 48 8D AC 24 ? ? ? ? 48 81 EC ? ? ? ? 48 8B 1D")
    def RenderHeadsUp(self, this: ctypes.c_uint64):
        pass


class cTkStopwatch(Structure):
    # OK FOR LATEST
    @function_hook("48 83 EC ? 48 8B 11 0F 29 74 24")
    # def GetDurationInSeconds(self, this: ctypes._Pointer["cTkStopwatch"]) -> ctypes.c_float:
    def GetDurationInSeconds(self, this: ctypes.c_uint64) -> ctypes.c_float:
        pass


class cGcApplication(Structure):
    @function_hook("40 53 48 83 EC 20 E8 ?? ?? ?? ?? 48 89")
    # def Update(self, this: ctypes._Pointer["cGcApplication"]):
    def Update(self, this: ctypes.c_uint64):
        pass


@partial_struct
class cGcPlanetGenerationInputData(Structure):
    Seed: Annotated[GcSeed, Field(GcSeed, 0x20)]

    @function_hook("48 89 5C 24 ? 57 48 83 EC ? 0F 57 C0 33 FF 0F 11 01 48 89 7C 24")
    def SetDefaults(self, this: ctypes._Pointer["cGcPlanetGenerationInputData"]):
        pass


@partial_struct
class cGcTerrainRegionMap(Structure):
    mfCachedRadius: Annotated[float, Field(ctypes.c_float, 0x30)]
    mMatrix: Annotated[cTkMatrix34, Field(cTkMatrix34, 0xD3490)]


@partial_struct
class GcEnvironmentProperties(Structure):
    AtmosphereEndHeight: Annotated[float, Field(ctypes.c_float, 0x1C)]
    AtmosphereStartHeight: Annotated[float, Field(ctypes.c_float, 0x20)]
    SkyAtmosphereHeight: Annotated[float, Field(ctypes.c_float, 0x5C)]
    StratosphereHeight: Annotated[float, Field(ctypes.c_float, 0x78)]


@partial_struct
class GcPlanetSkyProperties(Structure):
    pass


@partial_struct
class cGcPlanet(Structure):
    # Most of these found in cGcPlanet::Construct or cGcPlanet::cGcPlanet
    miPlanetIndex: Annotated[int, Field(ctypes.c_int32, 0x50)]
    mPlanetGenerationInputData: Annotated[
        cGcPlanetGenerationInputData,
        Field(cGcPlanetGenerationInputData, 0x3A40)
    ]
    mRegionMap: Annotated[cGcTerrainRegionMap, Field(cGcTerrainRegionMap, 0x3B70)]
    mNode: Annotated[TkHandle, Field(TkHandle, 0xD73B8)]
    mPosition: Annotated[Vector3f, Field(Vector3f, 0xD73D0)]

    mpEnvProperties: Annotated[
        ctypes._Pointer[GcEnvironmentProperties],
        Field(ctypes._Pointer[GcEnvironmentProperties], 0xD9038)
    ]
    mpSkyProperties: Annotated[
        ctypes._Pointer[GcPlanetSkyProperties],
        Field(ctypes._Pointer[GcPlanetSkyProperties], 0xD9040)
    ]

    @function_hook("48 89 5C 24 ? 48 89 6C 24 ? 48 89 74 24 ? 57 41 56 41 57 48 83 EC ? 45 33 FF 48 C7 41 ? ? ? ? ? 44 89 79")
    def cGcPlanet(self, this: ctypes._Pointer["cGcPlanet"]):
        pass

    @function_hook("48 89 5C 24 ? 48 89 74 24 ? 57 48 83 EC ? 33 F6 89 51 ? 89 B1")
    def Construct(self, this: ctypes._Pointer["cGcPlanet"], liIndex: ctypes.c_int32):
        pass

    @function_hook("48 89 5C 24 ? 48 89 6C 24 ? 56 57 41 56 48 83 EC ? 48 8B D9 8B 89")
    # def SetupRegionMap(self, this: ctypes._Pointer["cGcPlanet"]):
    def SetupRegionMap(self, this: ctypes.c_uint64):
        pass


@partial_struct
class cGcSolarSystem(Structure):
    mSolarSystemData: Annotated[cGcSolarSystemData, Field(cGcSolarSystemData, 0x0)]
    maPlanets: Annotated[list["cGcPlanet"], Field(cGcPlanet * 6, 0x2630)]

    @function_hook("48 89 5C 24 ? 48 89 74 24 ? 57 48 83 EC ? 0F 29 74 24 ? 48 8B D9 48 8B F9")
    def cGcSolarSystem(self, this: ctypes._Pointer["cGcSolarSystem"]):
        pass

    @function_hook("48 8B C4 48 89 58 ? 48 89 48 ? 55 56 57 41 54 41 55 41 56 41 57 48 8D A8 ? ? ? ? 48 81 EC ? ? ? ? 83 3D")
    def Construct(self, this: ctypes._Pointer["cGcSolarSystem"]):
        pass

    @function_hook("48 89 5C 24 ? 55 56 57 41 55 41 57 48 8B EC 48 83 EC ? 83 3D")
    # def OnLeavePlanetOrbit(self, this: ctypes._Pointer["cGcSolarSystem"], lbAnnounceOSD: ctypes.c_bool):
    def OnLeavePlanetOrbit(self, this: ctypes.c_uint64, lbAnnounceOSD: ctypes.c_bool):
        """lbAnnounceOSD not used."""
        pass

    @function_hook("48 8B C4 55 41 54 48 8D A8 ? ? ? ? 48 81 EC ? ? ? ? 48 89 58 ? 45 33 E4 44 39 25")
    # def OnEnterPlanetOrbit(self, this: ctypes._Pointer["cGcSolarSystem"], lbAnnounceOSD: ctypes.c_bool):
    def OnEnterPlanetOrbit(self, this: ctypes.c_uint64, lbAnnounceOSD: ctypes.c_bool):
        pass

    @function_hook("48 8B C4 48 89 58 ? 55 56 57 41 54 41 55 41 56 41 57 48 8D A8 ? ? ? ? 48 81 EC ? ? ? ? 0F 29 70 ? 41 BC")
    def Generate(
        self,
        # this: ctypes._Pointer["cGcSolarSystem"],
        this: ctypes.c_uint64,
        lbUseSettingsFile: ctypes.c_bool,
        lSeed: ctypes._Pointer[GcSeed]
    ):
        pass

class cGcApplicationLocalLoadState(Structure):
    @function_hook("48 89 5C 24 ? 57 48 83 EC ? 80 B9 ? ? ? ? ? 48 8B F9 BB")
    # def GetRespawnReason(self, this: ctypes._Pointer["cGcApplicationLocalLoadState"]) -> ctypes.c_int64:
    def GetRespawnReason(self, this: ctypes.c_uint64) -> ctypes.c_int64:
        pass


class cTkDynamicGravityControl(Structure):
    class cTkGravityPoint(ctypes.Structure):
        center: Vector3f
        strength: float
        falloffRadiusSqr: float
        maxStrength: float

    cTkGravityPoint._fields_ = [
        ("center", Vector3f),
        ("strength", ctypes.c_float),
        ("falloffRadiusSqr", ctypes.c_float),
        ("maxStrength", ctypes.c_float),
        ("padding0x1C", ctypes.c_ubyte * 0x4),
    ]

    class cTkGravityOBB(ctypes.Structure):
        up: Vector3f
        constantStrength: float
        falloffStrength: float
        transformInverse: cTkMatrix34
        untransformedCentre: Vector3f
        OBB: cTkAABB
        falloffRadiusSqr: float

    cTkGravityOBB._fields_ = [
        ("up", Vector3f),
        ("constantStrength", ctypes.c_float),
        ("falloffStrength", ctypes.c_float),
        # TODO: Add padding
        ("transformInverse", cTkMatrix34),
        ("untransformedCentre", Vector3f),
        ("OBB", cTkAABB),
        ("falloffRadiusSqr", ctypes.c_float),
    ]

    gravityPoints: list["cTkDynamicGravityControl.cTkGravityPoint"]
    numGravityPoints: int
    gravityOBBs: bytes

    @function_hook("33 C0 48 8D 91 ? ? ? ? 89 81")
    # def Construct(self, this: ctypes._Pointer["cTkDynamicGravityControl"]):
    def Construct(self, this: ctypes.c_uint64):
        pass

    @function_hook("4C 8B C1 48 8B C1 BA ? ? ? ? 0F 57 C0")
    def cTkDynamicGravityControl(self, this: ctypes.c_uint64):
        pass

    @function_hook("48 8B C4 55 57 41 54 41 55 48 81 EC")
    def GetGravity(
        self,
        this: ctypes.c_uint64,
        result: ctypes.c_uint64,
        lPos: ctypes.c_uint64
    ) -> ctypes.c_uint64:
        pass

cTkDynamicGravityControl._fields_ = [
    ("gravityPoints", cTkDynamicGravityControl.cTkGravityPoint * 0x9),
    ("numGravityPoints", ctypes.c_int32),
    ("gravityOBBs", cTkClassPool[cTkDynamicGravityControl.cTkGravityOBB, 0x40]),
]

# 141845_0x1D5C25_ST76561197993348610

@partial_struct
class GcSolarSystemData(Structure):
    PlanetOrbits: Annotated[list[int], Field(ctypes.c_int32 * 8, 0x21D0)]


class cGcSimulation(Structure):
    @function_hook("48 89 5C 24 ? 55 56 57 41 54 41 55 41 56 41 57 48 8D 6C 24 ? 48 81 EC ? ? ? ? 45 33 FF")
    def Construct(self, this: ctypes._Pointer["cGcSimulation"]):
        pass


@partial_struct
class cGcPlayerEnvironment(Structure):
    mPlayerTM: Annotated[cTkMatrix34, Field(cTkMatrix34, 0x0)]
    mUp: Annotated[Vector3f, Field(Vector3f, 0x40)]

    miNearestPlanetIndex: Annotated[int, Field(ctypes.c_uint32, 0x2BC)]
    mfDistanceFromPlanet: Annotated[float, Field(ctypes.c_float, 0x2C0)]
    mfNearestPlanetSealevel: Annotated[float, Field(ctypes.c_float, 0x2C4)]
    mNearestPlanetPos: Annotated[Vector3f, Field(Vector3f, 0x2D0)]
    mbInsidePlanetAtmosphere: Annotated[bool, Field(ctypes.c_bool, 0x2EC)]

    @function_hook("48 83 EC ? 80 B9 ? ? ? ? ? C6 04 24")
    def IsOnboardOwnFreighter(self, this: ctypes._Pointer["cGcPlayerEnvironment"]):
        pass

    @function_hook("8B 81 ? ? ? ? 83 E8 ? 83 F8 ? 0F 96 C0 C3 4C 8B D1")
    def IsOnPlanet(self, this: ctypes._Pointer["cGcPlayerEnvironment"]):
        pass

    @function_hook("48 8B C4 F3 0F 11 48 ? 55 53 57 41 56 48 8D A8")
    def Update(self, this: ctypes.c_uint64, lfTimeStep: ctypes.c_float):
        pass


class Engine:
    @static_function_hook("40 53 48 83 EC ? 44 8B D1 44 8B C1")
    def ShiftAllTransformsForNode(node: ctypes.c_uint32, lShift: ctypes.c_uint64):
        pass


def ShiftAllTransformsForNode(node: TkHandle, shift: Vector3f):
    Engine.ShiftAllTransformsForNode(node.lookupInt, get_addressof(shift))


newton_logger = logging.getLogger("Newton")


orbitParams = namedtuple("orbitParams", ["a", "b", "alpha", "delta"])


class RespawnReason(IntEnum):
    FreshStart = 0x0
    LoadSave = 0x1
    LoadToLocation = 0x2
    RestorePreviousSave = 0x3
    Unknown = 0x4
    DeathInSpace = 0x5
    DeathOnPlanet = 0x6
    DeathInOrbit = 0x7
    DeathOnAbandonedFreighter = 0x8
    WarpInShip = 0x9
    Teleport = 0xA
    Portal = 0xB
    UpgradeSaveAfterPatch = 0xC
    SwitchAmbientPlanet = 0xD
    BaseViewerMode = 0xE
    WarpInFreighter = 0xF
    JoinMultiplayer = 0x10


@dataclass
class NewtonState(ModState):
    planet_times: list[float]
    stopped_planet_index: int
    fixed_planet_position: Vector3f
    solar_system_center: Vector3f
    fixed_center: Vector3f
    planet_handles: list[Optional[TkHandle]]
    planets: list[Optional[cGcPlanet]]
    planet_periods: list[str]
    orbit_params: list[Optional[orbitParams]]
    parent_planet_map: list[int]
    planet_indexes: set[int]
    moon_indexes: set[int]
    planet_seeds: list[int]
    planets_moving: bool = False
    is_in_orbit: bool = False
    loaded_enough: bool = False
    gravity_singleton: Optional[cTkDynamicGravityControl] = None
    player_environment: cGcPlayerEnvironment = None
    orbital_period_buffers: list[ctypes.Array[ctypes.c_char]] = None


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
    TODO: Have a normal vector arg or something to make orbits more interesting.
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


class Newton(Mod):
    __author__ = "monkeyman192"
    __description__ = "Moving planets"
    __version__ = "0.2.0"

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
        gravity_singleton=None,
        player_environment=None,
        orbital_period_buffers=[None] * 8,
    )

    def __init__(self):
        super().__init__()
        self._time_rate = 1
        self.switch = 0
        self.lastRenderTimeMS = 0
        self._paused = True

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

        self._solarsystem_data: cGcSolarSystemData = None
        self._cached_hud_ptr = 0
        self._cached_hud: cGcShipHUD = None
        self._cached_period_text_element: cGcNGuiText = None

        self._solarsystem_data_ptr = 0

    @property
    @BOOLEAN("Simulation paused: ")
    def simulation_paused(self):
        return self._paused
    
    @simulation_paused.setter
    def simulation_paused(self, value):
        self._paused = value

    @property
    @FLOAT("Time rate: ")
    def time_rate(self):
        return self._time_rate

    @time_rate.setter
    def time_rate(self, value):
        self._time_rate = value

    def update_gravity_center(self, index: int, new_position: Vector3f):
        if self.state.gravity_singleton is not None:
            center = self.state.gravity_singleton.gravityPoints[index].center
            center.x = new_position.x
            center.y = new_position.y
            center.z = new_position.z

    def move_planet(self, index: int, new_position: Vector3f):
        planet = self.state.planets[index]
        handle = self.state.planet_handles[index]
        if planet is not None and handle is not None:
            delta = new_position - planet.mPosition
            planet.mPosition = new_position
            planet.mRegionMap.mMatrix.pos = new_position
            ShiftAllTransformsForNode(handle, delta)
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

    @one_shot
    @cTkDynamicGravityControl.Construct.after
    def load_gravity_singleton(self, this):
        if self.state.gravity_singleton is None:
            self.state.gravity_singleton = map_struct(this, cTkDynamicGravityControl)

    @one_shot
    @cTkDynamicGravityControl.cTkDynamicGravityControl.after
    def load_gravity_singleton2(self, this):
        if self.state.gravity_singleton is None:
            self.state.gravity_singleton = map_struct(this, cTkDynamicGravityControl)

    @one_shot
    @cTkDynamicGravityControl.GetGravity.after
    def load_gravity_singleton3(self, this, *args):
        if self.state.gravity_singleton is None:
            self.state.gravity_singleton = map_struct(this, cTkDynamicGravityControl)

    @cGcSolarSystem.Generate.after
    def generate_solarsystem(self, this: int, *args):
        self._solarsystem_data = map_struct(this, cGcSolarSystem).mSolarSystemData
        if self.state.gravity_singleton is None:
            newton_logger.warning("Falling back to hard-coded grav singleton...")
            self.state.gravity_singleton = map_struct(BASE_ADDRESS + 0x660D400, cTkDynamicGravityControl)
        # try:
        #     ssg = map_struct(this + 5364592, cGcSolarSystemGenerator)
        #     newton_logger.info(f"State 1: {ssg.RNG.state0}, State 2: {ssg.RNG.state1}")
        # except:
        #     newton_logger.exception(traceback.format_exc())

    @cGcApplicationLocalLoadState.GetRespawnReason.after
    def after_respawn(self, this, _result_):
        newton_logger.info(f"Starting to move the planets... Reason: {RespawnReason(_result_).name}")
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

    @cGcPlanet.SetupRegionMap.after
    def after_planet_setup(self, this):
        planet = map_struct(this, cGcPlanet)
        # Get some info about the planet and then store it so that we may access
        # it later.
        index = planet.miPlanetIndex
        self.state.planets[index] = planet
        self.state.planet_handles[index] = planet.mNode
        newton_logger.debug(f"Planet is index {index} at position {planet.mPosition}")
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

    @cGcShipHUD.RenderHeadsUp.before
    def before_render_HUD(self, this: int):
        # Check to see if the offset has changed. If it has update the cached
        # value and then re-cache.
        if self._cached_hud_ptr != this:
            self._cached_hud_ptr = this
            self._cached_hud = map_struct(this, cGcShipHUD)

            hud_root = self._cached_hud.mHeadsUpGUI.mRoot
            _text_layer = hud_root.FindTextRecursive(get_addressof(self.period_string_buffer))

            if _text_layer:
                self._cached_period_text_element = map_struct(_text_layer, cGcNGuiText)

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

    @property
    def nearest_planet_index(self) -> int:
        # Return the index of the nearest planet
        if self.state.player_environment is not None:
            return self.state.player_environment.miNearestPlanetIndex
        return -1

    @one_shot
    @cGcPlayerEnvironment.Update.after
    def get_player_env(self, this: int, lfTimeStep: float):
        self.state.player_environment = map_struct(this, cGcPlayerEnvironment)

    @cGcSolarSystem.OnEnterPlanetOrbit.after
    def after_enter_orbit(self, *args):
        # When we enter the orbit, do a sanity check and then set the fixed
        # planet position.
        if self.state.planets_moving:
            if self.nearest_planet_index != -1:
                self.state.is_in_orbit = True
                nearest_planet = self.state.planets[self.nearest_planet_index]
                if nearest_planet is not None:
                    self.state.fixed_planet_position = nearest_planet.mPosition

    @cTkStopwatch.GetDurationInSeconds.after
    def frame_time(self, this, _result_):
        # The main update loop doesn't have the delta time, so we'll get it here and then use it later.
        self.lastRenderTimeMS = _result_

    @cGcSolarSystem.OnLeavePlanetOrbit.after
    def after_exit_orbit(self, this, lbAnnounceOSD):
        self.state.fixed_center = self.state.solar_system_center
        self.state.fixed_planet_position = Vector3f(0, 0, 0)
        self.state.is_in_orbit = False

    def move_all_planets(self, delta: float):
        """ Move all the planets in the system. """
        nearest_planet_index = self.state.player_environment.miNearestPlanetIndex

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
                    parent_planet.mPosition,
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
                        parent_planet.mPosition,
                        self.state.orbit_params[idx],
                        self.state.planet_times[idx],
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

    @cGcApplication.Update.before
    def run_main_loop(self, this):
        if not self.run:
            return
        if self.state.loaded_enough and not self._paused:
            try:
                delta = self.time_rate * self.lastRenderTimeMS
                self.move_all_planets(delta)
            except:
                newton_logger.exception(traceback.format_exc())
                self.run = False


if __name__ == "__main__":
    load_mod_file(__file__)