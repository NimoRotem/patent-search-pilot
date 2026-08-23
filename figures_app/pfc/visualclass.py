"""Which conventional symbol a disclosed component is drawn with.

**This is the one place the compiler uses a component's NAME to decide anything visual, and it
is deliberate.** The rule it appears to bend says: never invent the physical appearance of a
component because you recognise its name. That rule is about geometry — the shape, the
proportions, the features, the dimensions. It is not about notation.

A draughtsman who reads "induction coil 110" draws the coil symbol. That is not a claim about
how this particular coil looks; it is the standard notation for the class of thing the applicant
named, exactly as an arrowhead is the standard notation for a disclosed direction. Refusing to
use it produced pages of identical rectangles: every statement true, nothing communicated.

So the mapping here is deliberately shallow, and three properties keep it honest:

* it maps to a SYMBOL, never to a dimension, a count or a feature. The coil symbol has five
  turns because five reads well, not because the patent said five;
* a name that matches nothing gets ``generic_component``, a plain outline, and most names do;
* the match is on the head noun of the drafter's own phrase, so it fires on what the document
  actually called the thing rather than on what the compiler guessed it might be.

An extractor may override any of this from the text; this is the floor, not the ceiling.
"""
from __future__ import annotations

import re

# Head-noun keywords to symbol class. Ordered longest-first at match time, so "suction cup"
# beats "cup" and "conductive substrate" beats "substrate".
_KEYWORDS: dict[str, tuple[str, ...]] = {
    "coil": ("coil", "inductor", "winding", "solenoid", "induction coil"),
    "spring": ("spring", "biasing member", "resilient member"),
    "motor": ("motor", "engine", "rotor", "turbine", "drive unit", "servo"),
    "pump": ("pump", "compressor", "blower", "fan", "vacuum generator", "ejector"),
    "valve": ("valve", "throttle", "regulator", "damper"),
    "power": ("battery", "power supply", "power source", "cell", "accumulator",
              "capacitor", "supply"),
    "sensor": ("sensor", "detector", "transducer", "probe", "gauge", "thermocouple",
               "encoder", "switch", "meter", "camera", "microphone", "accelerometer"),
    "magnet": ("magnet", "magnetic element", "permanent magnet", "magnetic field generator"),
    "electrode": ("electrode", "contact pad", "terminal", "conductive layer"),
    "plate": ("plate", "sheet", "panel", "slab", "disc", "disk", "wall", "lid", "cover",
              "flange", "membrane", "diaphragm", "film", "foil"),
    "substrate": ("substrate", "layer", "board", "wafer", "laminate", "core"),
    "adhesive": ("adhesive", "glue", "bond", "bonding layer", "sealant", "resin", "epoxy"),
    "housing": ("housing", "enclosure", "casing", "case", "shell", "body", "cabinet",
                "container", "vessel", "tank", "reservoir"),
    "chamber": ("chamber", "cavity", "compartment", "recess", "pocket", "plenum"),
    "shaft": ("shaft", "axle", "spindle", "rod", "pin", "stem", "post", "mandrel"),
    # "line" is deliberately absent: a score line, a production line and a line of sight are
    # all lines, and drawing any of them as a pipe is worse than drawing a plain outline.
    "tube": ("tube", "pipe", "conduit", "hose", "duct", "channel", "passage", "manifold",
             "fluid line", "vacuum line"),
    "gear": ("gear", "pinion", "sprocket", "cog", "worm"),
    "bearing": ("bearing", "bushing", "race"),
    "roller": ("roller", "drum", "cylinder", "barrel"),
    "belt": ("belt", "chain", "cable", "strap", "drive band"),
    "conveyor": ("conveyor", "conveyer"),
    "piston": ("piston", "ram", "plunger", "cylinder assembly"),
    "actuator": ("actuator", "cylinder actuator", "linear actuator", "positioner"),
    "nozzle": ("nozzle", "outlet", "orifice", "jet", "injector", "spray head", "applicator"),
    "suction_cup": ("suction cup", "suction pad", "vacuum cup", "gripping pad", "sucker"),
    "fastener": ("fastener", "screw", "bolt", "rivet", "nut", "clamp", "clip", "anchor"),
    "seal": ("seal", "o-ring", "gasket", "washer", "grommet"),
    "filter": ("filter", "screen mesh", "sieve", "strainer", "cartridge"),
    "heater": ("heater", "heating element", "resistor", "furnace", "oven", "hot plate"),
    "display": ("display", "screen", "monitor", "touchscreen", "indicator"),
    "interface": ("interface", "user interface", "panel interface", "keypad", "console"),
    # Ahead of "interface", which draws a monitor. A release button on a vacuum gripper came out
    # as a desktop computer screen because a button had no class of its own to be sorted into.
    # "switch" stays with the sensor above it: in a mechanical patent it is far more often a
    # limit or pressure switch than something a hand presses.
    "button": ("button", "push button", "pushbutton", "release button", "knob", "trigger",
               "actuating button"),
    # "unit" and "module" are absent for the same reason as "line": a lifting unit is not a
    # processor, and an adhesive module is not a chip.
    "processor": ("processor", "controller", "microcontroller", "cpu", "chip", "circuit",
                  "logic", "computer", "asic", "fpga", "control unit", "processing unit"),
    "memory": ("memory", "memory device", "ram", "rom", "cache", "buffer"),
    "storage": ("storage", "database", "data store", "repository", "disk drive"),
    "antenna": ("antenna", "aerial", "transceiver", "transmitter", "receiver", "radio"),
    "network": ("network", "bus", "data link"),
    "lens": ("lens", "optic", "objective", "mirror", "prism"),
    "opening": ("opening", "aperture", "hole", "bore", "port", "slot", "vent", "window"),
    "connector": ("connector", "coupling", "joint", "fitting", "adapter", "socket", "plug",
                  "hinge", "bracket", "mount"),
    "workpiece": ("workpiece", "work piece", "object", "article", "part to be", "substrate to"),
    "wheel": ("wheel", "castor", "caster", "turntable"),
    "arm": ("arm", "boom", "linkage", "lever"),
    "beam": ("beam", "rail", "girder", "strut", "bar"),
    "frame": ("frame", "chassis", "carriage", "support structure", "platform", "table",
              "base", "stand"),
    "gripper": ("gripper", "gripping device", "end effector", "jaw", "chuck", "tong"),
    "cutter": ("cutter", "blade", "knife", "saw", "drill"),
}

# Built once: (word tuple, class), longest phrase first so "suction cup" beats "cup".
_PHRASES: list[tuple[tuple[str, ...], str]] = sorted(
    ((tuple(phrase.split()), klass)
     for klass, phrases in _KEYWORDS.items() for phrase in phrases),
    key=lambda item: (-len(item[0]), -len(" ".join(item[0])), item[0]))

_WORD = re.compile(r"[a-z]+")


def _singular(word: str) -> str:
    """Enough plural handling to match "the coils" against "coil"."""
    if len(word) > 4 and word.endswith("ies"):
        return word[:-3] + "y"
    if len(word) > 4 and word.endswith(("ches", "shes", "sses", "xes", "zes")):
        return word[:-2]
    if len(word) > 3 and word.endswith("s") and not word.endswith(("ss", "us", "is")):
        return word[:-1]
    return word


def _contains(words: tuple[str, ...], needle: tuple[str, ...]) -> bool:
    """Whole-word containment, never substring.

    Substring matching looks equivalent and is a trap: "duct" is inside "induction", so an
    "induction-assisted adhesive activation system" was classified as a conduit and drawn as a
    pipe. The same hole hides "cell" in "excellent", "pin" in "spinning", "bar" in "barrier",
    "rod" in "produce" and "core" in "score".
    """
    span = len(needle)
    return any(words[start:start + span] == needle
               for start in range(len(words) - span + 1))

# A class that describes what a thing DOES rather than what it is made of. When a name matches
# both, the material reading loses: "conductive substrate" is a substrate, not a conductor.
_MATERIAL_QUALIFIERS = frozenset({"conductive", "insulating", "metallic", "magnetic",
                                  "dielectric", "adhesive", "thermal", "optical"})


def classify(name: str, aliases: tuple[str, ...] = ()) -> str:
    """The symbol class for a component's disclosed name, or ``generic_component``.

    Matched on the whole phrase first, so a multi-word name resolves to the specific symbol
    ("suction cup" rather than "cup"). A qualifier that describes a material is not allowed to
    steer the choice: "conductive substrate" is drawn as a substrate.
    """
    for candidate in (name, *aliases):
        words = tuple(_singular(word) for word in _WORD.findall(str(candidate or "").lower()))
        if not words:
            continue
        for needle, klass in _PHRASES:
            if not _contains(words, needle):
                continue
            # "adhesive substrate" names a substrate made of adhesive. The noun wins.
            if klass == "adhesive" and any(
                    word in words for word in ("substrate", "layer", "sheet", "film")):
                continue
            if len(needle) == 1 and needle[0] in _MATERIAL_QUALIFIERS:
                continue
            return klass
    return "generic_component"


def classify_all(entities) -> int:
    """Give every entity still on the default a class from its own name. Returns how many."""
    changed = 0
    for entity in entities:
        if entity.visual_class != "generic_component":
            continue
        klass = classify(entity.canonical_name, tuple(entity.aliases or ()))
        if klass != "generic_component":
            entity.visual_class = klass
            changed += 1
    return changed
