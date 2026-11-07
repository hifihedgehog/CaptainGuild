"""Currency and unit conversions: the unit tables, the names people type for currencies, and the readers that find a conversion in a sentence.

Each reader goes over a message once from left to right, so a long message costs time in proportion to its length. The first amount in a
message with the right words after it decides what's asked. Nothing here touches the network: the exchange rates come in as a mapping.
"""
from __future__ import annotations

import bisect
import math
import re
from typing import Mapping, NamedTuple

# Each table turns a unit into its category's base unit by multiplying. The keys are lowercase letters only, since a typed unit is read that way.

LENGTH = {  # meters
    "um": 0.000001, "micron": 0.000001, "microns": 0.000001,
    "micrometer": 0.000001, "micrometers": 0.000001, "micrometre": 0.000001, "micrometres": 0.000001,
    "nanometer": 0.000000001, "nanometers": 0.000000001, "nanometre": 0.000000001, "nanometres": 0.000000001,
    "thou": 0.0000254, "thous": 0.0000254,
    "mm": 0.001, "millimeter": 0.001, "millimeters": 0.001, "millimetre": 0.001, "millimetres": 0.001,
    "cm": 0.01, "centimeter": 0.01, "centimeters": 0.01, "centimetre": 0.01, "centimetres": 0.01,
    "m": 1.0, "meter": 1.0, "meters": 1.0, "metre": 1.0, "metres": 1.0,
    "km": 1000.0, "kilometer": 1000.0, "kilometers": 1000.0, "kilometre": 1000.0, "kilometres": 1000.0,
    "in": 0.0254, "inch": 0.0254, "inches": 0.0254,
    "ft": 0.3048, "foot": 0.3048, "feet": 0.3048,
    "yd": 0.9144, "yard": 0.9144, "yards": 0.9144,
    "mi": 1609.344, "mile": 1609.344, "miles": 1609.344,
    "furlong": 201.168, "furlongs": 201.168,
    "nmi": 1852.0, "nauticalmile": 1852.0, "nauticalmiles": 1852.0,
    "ly": 9460730472580800.0, "lightyear": 9460730472580800.0, "lightyears": 9460730472580800.0,
}

MASS = {  # kilograms
    "mcg": 0.000000001, "microgram": 0.000000001, "micrograms": 0.000000001,
    "mg": 0.000001, "milligram": 0.000001, "milligrams": 0.000001,
    "g": 0.001, "gram": 0.001, "grams": 0.001,
    "kg": 1.0, "kilogram": 1.0, "kilograms": 1.0,
    "oz": 0.028349523125, "ounce": 0.028349523125, "ounces": 0.028349523125,
    "lb": 0.45359237, "lbs": 0.45359237, "pound": 0.45359237, "pounds": 0.45359237,
    "st": 6.35029318, "stone": 6.35029318, "stones": 6.35029318,
    "t": 1000.0, "tonne": 1000.0, "tonnes": 1000.0, "metricton": 1000.0, "metrictons": 1000.0,
    "shortton": 907.18474, "shorttons": 907.18474, "uston": 907.18474, "ustons": 907.18474,
    "longton": 1016.0469088, "longtons": 1016.0469088, "imperialton": 1016.0469088, "imperialtons": 1016.0469088,
}

VOLUME = {  # cubic meters
    "tsp": 0.00000492892159375, "teaspoon": 0.00000492892159375, "teaspoons": 0.00000492892159375,
    "tbsp": 0.0000147867647813, "tablespoon": 0.0000147867647813, "tablespoons": 0.0000147867647813,
    "ml": 0.000001, "milliliter": 0.000001, "milliliters": 0.000001, "millilitre": 0.000001, "millilitres": 0.000001,
    "cc": 0.000001, "cubiccentimeter": 0.000001, "cubiccentimeters": 0.000001, "cubiccentimetre": 0.000001, "cubiccentimetres": 0.000001,
    "cl": 0.00001, "centiliter": 0.00001, "centiliters": 0.00001, "centilitre": 0.00001, "centilitres": 0.00001,
    "floz": 0.0000295735295625, "fluidounce": 0.0000295735295625, "fluidounces": 0.0000295735295625,
    "cup": 0.0002365882365, "cups": 0.0002365882365,
    "pt": 0.000473176473, "pint": 0.000473176473, "pints": 0.000473176473,
    "qt": 0.000946352946, "quart": 0.000946352946, "quarts": 0.000946352946,
    "l": 0.001, "liter": 0.001, "liters": 0.001, "litre": 0.001, "litres": 0.001,
    "gal": 0.003785411784, "gallon": 0.003785411784, "gallons": 0.003785411784,
    "impgal": 0.00454609, "imperialgallon": 0.00454609, "imperialgallons": 0.00454609,
    # cubes written as mm³, cm³ and the like (see SHAPED)
    "cubicmillimeter": 0.000000001, "cubicmillimeters": 0.000000001, "cubicmeter": 1.0, "cubicmeters": 1.0, "cubicmetre": 1.0, "cubicmetres": 1.0,
    "cubicinch": 0.000016387064, "cubicinches": 0.000016387064, "cubicfoot": 0.028316846592, "cubicfeet": 0.028316846592,
    "cubicyard": 0.764554857984, "cubicyards": 0.764554857984,
}

AREA = {  # square meters
    "sqmm": 0.000001, "sqmillimeter": 0.000001, "sqmillimeters": 0.000001,
    "squaremillimeter": 0.000001, "squaremillimeters": 0.000001, "squaremillimetre": 0.000001, "squaremillimetres": 0.000001,
    "sqcm": 0.0001, "sqcentimeter": 0.0001, "sqcentimeters": 0.0001,
    "squarecentimeter": 0.0001, "squarecentimeters": 0.0001, "squarecentimetre": 0.0001, "squarecentimetres": 0.0001,
    "sqm": 1.0, "sqmeter": 1.0, "sqmeters": 1.0, "sqmetre": 1.0, "sqmetres": 1.0,
    "squaremeter": 1.0, "squaremeters": 1.0, "squaremetre": 1.0, "squaremetres": 1.0,
    "sqkm": 1000000.0, "sqkilometer": 1000000.0, "sqkilometers": 1000000.0, "sqkilometre": 1000000.0, "sqkilometres": 1000000.0,
    "squarekilometer": 1000000.0, "squarekilometers": 1000000.0, "squarekilometre": 1000000.0, "squarekilometres": 1000000.0,
    "ha": 10000.0, "hectare": 10000.0, "hectares": 10000.0,
    "sqin": 0.00064516, "sqinch": 0.00064516, "sqinches": 0.00064516,
    "squareinch": 0.00064516, "squareinches": 0.00064516,
    "sqft": 0.09290304, "sqfoot": 0.09290304, "sqfeet": 0.09290304,
    "squarefoot": 0.09290304, "squarefeet": 0.09290304,
    "sqyd": 0.83612736, "sqyard": 0.83612736, "sqyards": 0.83612736,
    "squareyard": 0.83612736, "squareyards": 0.83612736,
    "acre": 4046.8564224, "acres": 4046.8564224,
    "sqmi": 2589988.110336, "sqmile": 2589988.110336, "sqmiles": 2589988.110336,
    "squaremile": 2589988.110336, "squaremiles": 2589988.110336,
}

SPEED = {  # meters per second
    "mps": 1.0, "ms": 1.0, "meterspersecond": 1.0, "metrespersecond": 1.0,
    "mms": 0.001, "mmps": 0.001, "mmpersecond": 0.001,
    "mmmin": 0.001 / 60, "mmperminute": 0.001 / 60,
    "ins": 0.0254, "inpersecond": 0.0254,
    "inmin": 0.0254 / 60, "inperminute": 0.0254 / 60,
    "kmh": 0.2777777777777778, "kph": 0.2777777777777778,
    "kilometersperhour": 0.2777777777777778, "kilometresperhour": 0.2777777777777778,
    "mph": 0.44704, "milesperhour": 0.44704,
    "fps": 0.3048, "fts": 0.3048, "feetpersecond": 0.3048, "footpersecond": 0.3048,
    "knot": 0.5144444444444444, "knots": 0.5144444444444444, "kts": 0.5144444444444444,
}

TIME = {  # seconds
    "ms": 0.001, "millisecond": 0.001, "milliseconds": 0.001,
    "s": 1.0, "sec": 1.0, "second": 1.0, "seconds": 1.0,
    "min": 60.0, "minute": 60.0, "minutes": 60.0,
    "hr": 3600.0, "hour": 3600.0, "hours": 3600.0,
    "day": 86400.0, "days": 86400.0,
    "week": 604800.0, "weeks": 604800.0,
    "month": 2629746.0, "months": 2629746.0,
    "year": 31556952.0, "years": 31556952.0, "yr": 31556952.0,
}

STORAGE = {  # bytes
    "bit": 0.125, "bits": 0.125,
    "byte": 1.0, "bytes": 1.0, "b": 1.0,
    "kb": 1000.0, "kilobyte": 1000.0, "kilobytes": 1000.0,
    "mb": 1000000.0, "megabyte": 1000000.0, "megabytes": 1000000.0,
    "gb": 1000000000.0, "gigabyte": 1000000000.0, "gigabytes": 1000000000.0,
    "tb": 1000000000000.0, "terabyte": 1000000000000.0, "terabytes": 1000000000000.0,
    "pb": 1000000000000000.0, "petabyte": 1000000000000000.0, "petabytes": 1000000000000000.0,
    "kib": 1024.0, "kibibyte": 1024.0, "kibibytes": 1024.0,
    "mib": 1048576.0, "mebibyte": 1048576.0, "mebibytes": 1048576.0,
    "gib": 1073741824.0, "gibibyte": 1073741824.0, "gibibytes": 1073741824.0,
    "tib": 1099511627776.0, "tebibyte": 1099511627776.0, "tebibytes": 1099511627776.0,
    "pib": 1125899906842624.0, "pebibyte": 1125899906842624.0, "pebibytes": 1125899906842624.0,
    # bits by the thousand, as Kb, Mb, Gb, Tb and Pb mean (see CASE_SYMBOLS)
    "kilobit": 125.0, "kilobits": 125.0, "megabit": 125000.0, "megabits": 125000.0, "gigabit": 125000000.0, "gigabits": 125000000.0,
    "terabit": 125000000000.0, "terabits": 125000000000.0, "petabit": 125000000000000.0, "petabits": 125000000000000.0,
}

PRESSURE = {  # pascals
    "pa": 1.0, "pascal": 1.0, "pascals": 1.0,
    "hpa": 100.0, "hectopascal": 100.0, "hectopascals": 100.0,
    "kpa": 1000.0, "kilopascal": 1000.0, "kilopascals": 1000.0,
    "bar": 100000.0, "bars": 100000.0,
    "mbar": 100.0, "millibar": 100.0, "millibars": 100.0,
    "psi": 6894.757293168, "psia": 6894.757293168, "poundspersquareinch": 6894.757293168,
    "atm": 101325.0, "atmosphere": 101325.0, "atmospheres": 101325.0,
    "mmhg": 133.322387415, "millimetersofmercury": 133.322387415, "millimetresofmercury": 133.322387415,
    "torr": 133.322368421, "torrs": 133.322368421,
    "inhg": 3386.389, "inchesofmercury": 3386.389,
}

ENERGY = {  # joules
    "j": 1.0, "joule": 1.0, "joules": 1.0,
    "kj": 1000.0, "kilojoule": 1000.0, "kilojoules": 1000.0,
    "cal": 4.184, "calorie": 4.184, "calories": 4.184,
    "kcal": 4184.0, "kilocalorie": 4184.0, "kilocalories": 4184.0,
    "wh": 3600.0, "watthour": 3600.0, "watthours": 3600.0,
    "kwh": 3600000.0, "kilowatthour": 3600000.0, "kilowatthours": 3600000.0,
    "btu": 1055.05585262, "btus": 1055.05585262, "britishthermalunit": 1055.05585262, "britishthermalunits": 1055.05585262,
    "ev": 1.602176634e-19, "electronvolt": 1.602176634e-19, "electronvolts": 1.602176634e-19,
    "ftlbf": 1.3558179483314,
}

POWER = {  # watts
    "w": 1.0, "watt": 1.0, "watts": 1.0,
    "milliwatt": 0.001, "milliwatts": 0.001,
    "kw": 1000.0, "kilowatt": 1000.0, "kilowatts": 1000.0,
    "mw": 1000000.0, "megawatt": 1000000.0, "megawatts": 1000000.0,
    "hp": 745.69987158, "horsepower": 745.69987158,
    "btuh": 0.29307107, "btuhr": 0.29307107,
}

FORCE = {  # newtons
    "n": 1.0, "newton": 1.0, "newtons": 1.0,
    "kn": 1000.0, "kilonewton": 1000.0, "kilonewtons": 1000.0,
    "lbf": 4.4482216152605, "poundforce": 4.4482216152605, "poundforces": 4.4482216152605,
    "kgf": 9.80665, "kilogramforce": 9.80665, "kilogramforces": 9.80665,
    "dyn": 0.00001, "dyne": 0.00001, "dynes": 0.00001,
}

TORQUE = {  # newton-meters
    "nm": 1.0, "newtonmeter": 1.0, "newtonmeters": 1.0, "newtonmetre": 1.0, "newtonmetres": 1.0,
    "ftlb": 1.3558179483314, "ftlbs": 1.3558179483314, "footpound": 1.3558179483314, "footpounds": 1.3558179483314,
    "inlb": 0.1129848290276, "inlbs": 0.1129848290276, "inchpound": 0.1129848290276, "inchpounds": 0.1129848290276,
}

ANGLE = {  # degrees
    "deg": 1.0, "degree": 1.0, "degrees": 1.0,
    "rad": 57.29577951308232, "radian": 57.29577951308232, "radians": 57.29577951308232,
    "grad": 0.9, "gradian": 0.9, "gradians": 0.9, "gon": 0.9,
    "arcmin": 1 / 60, "arcminute": 1 / 60, "arcminutes": 1 / 60,
    "arcsec": 1 / 3600, "arcsecond": 1 / 3600, "arcseconds": 1 / 3600,
    "rev": 360.0, "revolution": 360.0, "revolutions": 360.0, "turn": 360.0, "turns": 360.0,
}

DATA_RATE = {  # bits per second
    "bps": 1.0, "bitspersecond": 1.0,
    "kbps": 1000.0, "kilobitspersecond": 1000.0,
    "mbps": 1000000.0, "megabitspersecond": 1000000.0,
    "gbps": 1000000000.0, "gigabitspersecond": 1000000000.0,
    "bytesps": 8.0, "bytespersecond": 8.0,
    "kbytesps": 8000.0, "kilobytespersecond": 8000.0,
    "mbytesps": 8000000.0, "megabytespersecond": 8000000.0,
    "gbytesps": 8000000000.0, "gigabytespersecond": 8000000000.0,
}

FUEL = {  # kilometers per liter
    "kml": 1.0, "kmperliter": 1.0, "kmperl": 1.0, "kilometersperliter": 1.0, "kilometresperliter": 1.0, "kilometresperlitre": 1.0,
    "mpg": 0.425143707, "milespergallon": 0.425143707,
    "impmpg": 0.354006044, "imperialmpg": 0.354006044,
}

PRINT_SPEED = {  # millimeters per second
    "mmps": 1.0, "mms": 1.0, "mmpersec": 1.0, "mmpersecond": 1.0,
    "mmpm": 1 / 60, "mmmin": 1 / 60, "mmpermin": 1 / 60, "mmperminute": 1 / 60,
    "inps": 25.4, "ins": 25.4, "inpersec": 25.4, "inpersecond": 25.4,
    "inpm": 25.4 / 60, "inmin": 25.4 / 60, "inpermin": 25.4 / 60,
}

FLOW_RATE = {  # cubic millimeters per second
    "mmcubedps": 1.0, "mmcubedpersec": 1.0, "mms": 1.0,
    "cmcubedpm": 1000 / 60, "ccpermin": 1000 / 60, "ccmin": 1000 / 60,
}

DENSITY = {  # grams per cubic centimeter
    "gpercm": 1.0, "gcm": 1.0, "gpercc": 1.0,
    "kgperm": 0.001, "kgm": 0.001,  # 1 kg/m³ is 1,000 g in 1,000,000 cm³
    "lbperin": 27.679904710191, "lbin": 27.679904710191,
    "lbperft": 0.01601846337396, "lbft": 0.01601846337396,
}

# Tried in this order, so a name two tables share, such as "ms", means the first table holding both units of a pair.
RATIO_TABLES = (LENGTH, MASS, VOLUME, AREA, SPEED, TIME, STORAGE, PRESSURE, ENERGY, POWER, FORCE, TORQUE, ANGLE, DATA_RATE, FUEL, PRINT_SPEED, FLOW_RATE, DENSITY)
CELSIUS, FAHRENHEIT, KELVIN = {"c", "celsius"}, {"f", "fahrenheit"}, {"k", "kelvin"}

# What the unit options suggest as someone types.
UNIT_CHOICES = sorted(set(
    ["C", "°C", "celsius", "F", "°F", "fahrenheit", "K", "kelvin"]
    + ["um", "mm", "cm", "m", "km", "in", "inch", "ft", "foot", "yd", "mi", "mile", "nmi", "thou", "micron", "nanometer", "furlong", "lightyear", "ly"]
    + ["mcg", "mg", "g", "kg", "oz", "lb", "lbs", "stone", "tonne", "shortton", "longton"]
    + ["tsp", "tbsp", "ml", "cc", "cl", "fl oz", "cup", "pt", "pint", "qt", "quart", "l", "liter", "gal", "gallon", "imp gal"]
    + ["sq mm", "sq cm", "sq m", "sq km", "sq in", "sq ft", "sq yd", "sq mi", "square meter", "square foot", "square mile", "hectare", "acre"]
    + ["m/s", "mps", "km/h", "mph", "fps", "knots"]
    + ["ms", "s", "sec", "min", "hr", "hour", "day", "week", "month", "year"]
    + ["bit", "byte", "KB", "MB", "GB", "TB", "PB", "KiB", "MiB", "GiB", "TiB"]
    + ["Pa", "hPa", "kPa", "bar", "mbar", "psi", "atm", "mmHg", "inHg", "torr"]
    + ["J", "kJ", "cal", "kcal", "Wh", "kWh", "BTU", "eV"]
    + ["W", "kW", "MW", "hp"]
    + ["N", "kN", "lbf", "kgf", "dyne"]
    + ["Nm", "ft-lb", "in-lb"]
    + ["deg", "rad", "grad", "arcmin", "arcsec", "rev", "turn"]
    + ["bps", "Kbps", "Mbps", "Gbps"]
    + ["km/L", "mpg", "imp mpg"]
    + ["mm/s", "mm/min", "in/s", "in/min", "mm³/s", "cc/min", "g/cm³", "kg/m³", "lb/in³", "lb/ft³"]
))

UNITS_HELP = (
    "Unsupported conversion. Supported categories:\n"
    "- **Temperature:** °C ↔ °F ↔ K\n"
    "- **Length:** µm/mm/cm/m/km ↔ thou/in/ft/yd/mi/nmi\n"
    "- **Mass:** mcg/mg/g/kg/tonne ↔ oz/lb/stone\n"
    "- **Volume:** tsp/tbsp/ml/cl/fl oz/cup/pt/qt/l/gal\n"
    "- **Area:** sq mm/cm/m/km ↔ sq in/ft/yd/mi, hectare, acre\n"
    "- **Speed:** m/s ↔ km/h ↔ mph ↔ knots\n"
    "- **Time:** ms/s/min/hr/day/week/month/year\n"
    "- **Storage:** bit/byte/KB/MB/GB/TB (+ KiB/MiB/GiB/TiB)\n"
    "- **Pressure:** Pa/kPa/bar/psi/atm/mmHg\n"
    "- **Energy:** J/kJ/cal/kcal/Wh/kWh/BTU\n"
    "- **Power:** W/kW/MW/hp\n"
    "- **Force:** N/kN/lbf/kgf\n"
    "- **Torque:** Nm ↔ ft-lb ↔ in-lb\n"
    "- **Angle:** deg/rad/grad/arcmin/arcsec/rev\n"
    "- **Data rate:** bps/Kbps/Mbps/Gbps\n"
    "- **Fuel:** km/L ↔ mpg\n"
    "- **3D Print speed:** mm/s ↔ mm/min ↔ in/s\n"
    "- **Density:** g/cm³ ↔ kg/m³ ↔ lb/in³"
)


# Symbols whose case changes the unit, as typed with the spaces taken out. Written all in one case, a symbol reads as the tables have it,
# so mb and MB are megabytes and mw and MW are megawatts, while Mb is megabits and mW is milliwatts.
CASE_SYMBOLS = {
    "mW": "milliwatt", "Cal": "kcal", "b": "bit", "B": "byte", "Kb": "kilobit", "Mb": "megabit", "Gb": "gigabit", "Tb": "terabit", "Pb": "petabit",
    "kBps": "kbytesps", "KBps": "kbytesps", "MBps": "mbytesps", "GBps": "gbytesps",
    "Mm": "",  # megameters by the symbol, millimeters by the likely slip, so no answer
}
# Squares and cubes written with a mark or a digit, such as m², m2 or cm³.
SHAPED = {
    "mm2": "sqmm", "cm2": "sqcm", "m2": "sqm", "km2": "sqkm", "in2": "sqin", "ft2": "sqft", "yd2": "sqyd", "mi2": "sqmi",
    "mm3": "cubicmillimeter", "cm3": "cc", "m3": "cubicmeter", "in3": "cubicinch", "ft3": "cubicfoot", "yd3": "cubicyard",
}
# Pound-feet and pound-inches are torque. Written with a slash, as lb/ft³, the same letters are a density.
UNSLASHED = {"lbft": "ftlb", "lbin": "inlb", "gcm": "", "kgm": ""}
_STRAY_DIGIT = re.compile(r"(?<![^\W\d_])\d")  # a digit with no letter right before it, as in "210 C" or "/4 inch"


def normalize_unit(unit: str) -> str:
    """The table key a typed unit means, or an empty string for no unit. The micro sign reads as u, a square or a cube must be one that
    SHAPED names, and a number anywhere else, as in "210 C", means the words aren't a unit at all. A unit with a slash, such as km/h or g/cm³,
    keeps only its letters."""
    compact = re.sub(r"\s+", "", unit)
    if compact in CASE_SYMBOLS:
        return CASE_SYMBOLS[compact]
    text = compact.replace("\u00b5", "u").replace("\u03bc", "u").replace("\u00b2", "2").replace("\u00b3", "3")
    if _STRAY_DIGIT.search(text):
        return ""
    if "/" in text:
        return re.sub(r"[^a-z]", "", text.lower())
    key = re.sub(r"[^a-z0-9]", "", text.lower())
    if any(char.isdigit() for char in key):
        return SHAPED.get(key, "")
    return UNSLASHED.get(key, key)


def convert_units(amount: float, source: str, target: str) -> float | None:
    """The amount in the target unit, or None when the two aren't units of one kind."""
    first, second = normalize_unit(source), normalize_unit(target)
    if not first or not second:
        return None
    temperatures = CELSIUS | FAHRENHEIT | KELVIN
    if first in temperatures and second in temperatures:
        if first in CELSIUS:
            kelvin = amount + 273.15
        elif first in FAHRENHEIT:
            kelvin = (amount - 32) * 5 / 9 + 273.15
        else:
            kelvin = amount
        if second in CELSIUS:
            return kelvin - 273.15
        if second in FAHRENHEIT:
            return (kelvin - 273.15) * 9 / 5 + 32
        return kelvin
    for table in RATIO_TABLES:
        if first in table and second in table:
            return amount * table[first] / table[second]
    return None


# Names people type for a currency, with the code each one means. A code itself is looked up in the rates.
CURRENCY_ALIASES = {
    "usd": "usd", "dollar": "usd", "dollars": "usd",
    "us dollar": "usd", "us dollars": "usd",
    "american dollar": "usd", "american dollars": "usd",
    "freedom farthing": "usd", "freedom farthings": "usd",
    "buck": "usd", "bucks": "usd",
    "greenback": "usd", "greenbacks": "usd",
    "dead presidents": "usd",
    "smackeroo": "usd", "smackeroos": "usd",
    "cad": "cad", "canadian dollar": "cad", "canadian dollars": "cad",
    "loonie": "cad", "loonies": "cad",
    "moose peso": "cad", "moose pesos": "cad",
    "maple buck": "cad", "maple bucks": "cad",
    "eur": "eur", "euro": "eur", "euros": "eur",
    "gbp": "gbp", "pound": "gbp", "pounds": "gbp",
    "british pound": "gbp", "british pounds": "gbp", "pound sterling": "gbp",
    "quid": "gbp", "quids": "gbp",
    "nicker": "gbp", "nickers": "gbp",
    "jpy": "jpy", "yen": "jpy",
    "cny": "cny", "yuan": "cny", "renminbi": "cny", "rmb": "cny",
    "aud": "aud", "australian dollar": "aud", "australian dollars": "aud", "aussie dollar": "aud",
    "aussie buck": "aud", "aussie bucks": "aud",
    "dollarydoo": "aud", "dollarydoos": "aud",
    "nzd": "nzd", "new zealand dollar": "nzd", "kiwi dollar": "nzd", "kiwi dollars": "nzd",
    "kiwi": "nzd",
    "chf": "chf", "swiss franc": "chf", "swiss francs": "chf", "swissie": "chf",
    "inr": "inr", "rupee": "inr", "rupees": "inr", "indian rupee": "inr",
    "krw": "krw", "won": "krw", "korean won": "krw",
    "sek": "sek", "swedish krona": "sek", "swedish kronor": "sek",
    "nok": "nok", "norwegian krone": "nok", "norwegian kroner": "nok",
    "dkk": "dkk", "danish krone": "dkk", "danish kroner": "dkk",
    "mxn": "mxn", "mexican peso": "mxn", "mexican pesos": "mxn",
    "pln": "pln", "zloty": "pln", "zlotys": "pln", "polish zloty": "pln",
    "czk": "czk", "koruna": "czk", "czech koruna": "czk",
    "huf": "huf", "forint": "huf", "forints": "huf", "hungarian forint": "huf",
    "try": "try", "lira": "try", "turkish lira": "try",
    "rub": "rub", "ruble": "rub", "rubles": "rub", "rouble": "rub", "roubles": "rub",
    "zar": "zar", "rand": "zar", "south african rand": "zar",
    "thb": "thb", "baht": "thb", "thai baht": "thb",
    "sgd": "sgd", "singapore dollar": "sgd", "singapore dollars": "sgd",
    "hkd": "hkd", "hong kong dollar": "hkd", "hong kong dollars": "hkd",
    "twd": "twd", "taiwan dollar": "twd", "taiwanese dollar": "twd",
    "php": "php", "philippine peso": "php", "philippine pesos": "php",
    "vnd": "vnd", "dong": "vnd", "vietnamese dong": "vnd",
    "myr": "myr", "ringgit": "myr", "malaysian ringgit": "myr",
    "ars": "ars", "argentine peso": "ars", "argentine pesos": "ars",
    "cop": "cop", "colombian peso": "cop", "colombian pesos": "cop",
    "brl": "brl", "real": "brl", "reais": "brl", "brazilian real": "brl",
    # cryptocurrencies by their common names
    "btc": "btc", "bitcoin": "btc", "satoshi": "sats",
    "eth": "eth", "ethereum": "eth", "ether": "eth",
    "doge": "doge", "dogecoin": "doge",
    "sol": "sol", "solana": "sol",
    "xrp": "xrp", "ripple": "xrp",
    "ltc": "ltc", "litecoin": "ltc",
    "ada": "ada", "cardano": "ada",
    "dot": "dot", "polkadot": "dot",
    "matic": "matic", "polygon": "matic",
    "shib": "shib", "shiba": "shib", "shiba inu": "shib",
    "avax": "avax", "avalanche": "avax",
    "atom": "atom", "cosmos": "atom",
    "link": "link", "chainlink": "link",
    "uni": "uni", "uniswap": "uni",
    "xlm": "xlm", "stellar": "xlm", "stellar lumens": "xlm",
    "algo": "algo", "algorand": "algo",
    "near": "near", "near protocol": "near",
    "ftm": "ftm", "fantom": "ftm",
    "aave": "aave",
    "mkr": "mkr", "maker": "mkr",
    "comp": "comp", "compound": "comp",
    "snx": "snx", "synthetix": "snx",
    "apt": "apt", "aptos": "apt",
    "arb": "arb", "arbitrum": "arb",
    "op": "op", "optimism": "op",
    "fil": "fil", "filecoin": "fil",
    "vet": "vet", "vechain": "vet",
    "hbar": "hbar", "hedera": "hbar",
    "icp": "icp", "internet computer": "icp",
    "sand": "sand", "sandbox": "sand",
    "mana": "mana", "decentraland": "mana",
    "axs": "axs", "axie infinity": "axs", "axie": "axs",
    "crv": "crv", "curve": "crv",
    "ldo": "ldo", "lido": "ldo",
    "ape": "ape", "apecoin": "ape",
    "grt": "grt", "the graph": "grt", "graph": "grt",
    "theta": "theta",
    "xtz": "xtz", "tezos": "xtz",
    "eos": "eos",
    "trx": "trx", "tron": "trx",
    "xmr": "xmr", "monero": "xmr",
    "zec": "zec", "zcash": "zec",
    "bat": "bat", "basic attention token": "bat",
    "enj": "enj", "enjin": "enj",
    "1inch": "1inch",
    "sushi": "sushi", "sushiswap": "sushi",
    "cake": "cake", "pancakeswap": "cake",
    "usdt": "usdt", "tether": "usdt",
    "usdc": "usdc", "usd coin": "usdc",
    "dai": "dai",
    "busd": "busd", "binance usd": "busd",
    "bnb": "bnb", "binance coin": "bnb", "binance": "bnb",
}

# Names the rate source leaves out.
MISSING_CURRENCY_NAMES = {
    "usdd": "Decentralized USD", "eurc": "Euro Coin", "kas": "Kaspa", "lunc": "Terra Classic", "mxv": "Mexican Unidad de Inversion",
    "ordi": "Ordinals", "pepe": "Pepe", "trl": "Turkish Lira (old)", "ved": "Venezuelan Bolívar Digital", "xbt": "Bitcoin", "zwg": "Zimbabwe Gold",
}


def currency_code(phrase: str, rates: Mapping[str, float] | None) -> str | None:
    """The code a typed name or code means, or None."""
    alias = CURRENCY_ALIASES.get(phrase.lower().replace(".", "").strip())
    if alias is not None:
        return alias
    code = phrase.lower().strip()
    return code if rates and code in rates else None


def currency_name(code: str, names: Mapping[str, str] | None) -> str:
    name = (names or {}).get(code, "")
    if isinstance(name, str) and name and name != code:
        return name
    return MISSING_CURRENCY_NAMES.get(code, "")


def currency_display(code: str, names: Mapping[str, str] | None) -> str:
    """"USD - US Dollar", or the code alone when the name isn't known."""
    name = currency_name(code, names)
    return f"{code.upper()} - {name}" if name else code.upper()


def currency_label(phrase: str, code: str, names: Mapping[str, str] | None) -> str:
    """"bucks (USD - US Dollar)" for a name someone typed, and "USD - US Dollar" when they typed the code."""
    if phrase.lower() == code:
        return currency_display(code, names)
    name = currency_name(code, names)
    return f"{phrase} ({code.upper()} - {name})" if name else f"{phrase} ({code.upper()})"


def currency_jokes(source: str, target: str) -> str:
    """A moose for moose pesos, and an eagle and a flag for freedom farthings."""
    both = (source.lower(), target.lower())
    found = ""
    if any(word in phrase for phrase in both for word in ("moose", "peso", "pesos")):
        found += " \U0001face"
    if any(word in phrase for phrase in both for word in ("freedom freedom", "farthing", "farthings")):
        found += " \U0001f985\U0001f1fa\U0001f1f8"
    return found


def figure(value: float, places: int) -> str:
    """A number with thousands separators and this many decimal places. A number so small it would read as zero gets the places it needs,
    and one far too small or too large for that is written as a power of ten."""
    size = abs(value)
    if size >= 1e21:
        return f"{value:.6e}"
    if value != 0 and size < 10 ** -places:
        needed = 2 - math.floor(math.log10(size))
        return f"{value:,.{needed}f}" if needed <= 20 else f"{value:.3e}"
    return f"{value:,.{places}f}"


class Asked(NamedTuple):
    """A conversion found in a sentence: the amount as typed, and the two names around the word to, in or into."""
    amount: str
    source: str
    target: str


# A number standing on its own: nothing word-like, no dot, comma or slash right before it, so a model name such as X1C holds no amount and the
# second half of 190-220 has no minus sign. A slash inside makes it a fraction such as 3/4, and a date such as 11/6/2026 is no amount. A whole
# number then a fraction, as in 1 1/2 or 1-1/2, is one mixed number.
_AMOUNT = re.compile(r"(?<![\w.,/])-?(?:\d++(?:[ \t]++|-)\d++/\d++(?![\d/])|[\d,./]+)")
_MIXED = re.compile(r"(-?)(\d+)(?:[ \t]+|-)(\d+)/(\d+)")
_FRACTION = re.compile(r"-?(\d+)/(\d+)")
_PLAIN_NUMBER = re.compile(r"-?(?:\d+\.?\d*|\.\d+)")  # 1, 1.5, .5
_COMMA_THOUSANDS = re.compile(r"-?\d{1,3}(?:,\d{3})+(?:\.\d+)?")  # 1,000 and 1,234.5
_DOT_THOUSANDS = re.compile(r"-?\d{1,3}(?:\.\d{3})+(?:,\d+)?")  # 1.234.567 and 1.234,5
_DECIMAL_COMMA = re.compile(r"-?\d*,\d+")  # 1,75, as much of Europe writes it
_DIGIT = re.compile(r"\d")
_NOT_SPACE = re.compile(r"\S")
_PHRASE_WORD = re.compile(r"[\w.]+")  # a currency phrase holds words of letters, digits and dots, with spaces between
_UNIT_WORD = re.compile(r"[\w°/³]+")  # a unit word also holds the degree sign, a slash and a cubed sign
CONNECTORS = frozenset({"to", "in", "into"})
# Two codes of three letters after an amount, as in "100 usd eur", "100USD to EUR" or "100 usd=eur". Each code is a whole word.
_CODES = re.compile(r"\s*+([A-Za-z]{3})(?![A-Za-z])\s*+(?:(?i:into|to|in)(?![A-Za-z])\s*+|=\s*+)?([A-Za-z]{3})(?![A-Za-z])")


def amounts(text: str) -> list[re.Match]:
    """Each number that can be an amount: a run of digits, commas and dots that holds a digit, a fraction, or a mixed number."""
    return [found for found in _AMOUNT.finditer(text)
            if _DIGIT.search(found.group()) and ("/" not in found.group() or _FRACTION.fullmatch(found.group()) or _MIXED.fullmatch(found.group()))]


def parse_amount(typed: str) -> float | None:
    """The number an amount means, or None for one that doesn't read as a finite number. A comma before three digits separates thousands, as
    in 1,000, and a comma before one, two or four or more is a decimal comma, as in 1,75. Where both marks appear, the last one is the decimal.
    A mixed number such as 1 1/2 or 1-1/2 is its whole number and its fraction added."""
    mixed = _MIXED.fullmatch(typed)
    fraction = _FRACTION.fullmatch(typed)
    try:
        if mixed:
            sign, whole, top, bottom = mixed.groups()
            if int(bottom) == 0:
                return None
            value = (int(whole) + int(top) / int(bottom)) * (-1 if sign else 1)
        elif fraction:
            if int(fraction.group(2)) == 0:
                return None
            value = int(fraction.group(1)) / int(fraction.group(2)) * (-1 if typed.startswith("-") else 1)
        elif _PLAIN_NUMBER.fullmatch(typed):
            value = float(typed)
        elif _COMMA_THOUSANDS.fullmatch(typed):
            value = float(typed.replace(",", ""))
        elif _DOT_THOUSANDS.fullmatch(typed):
            value = float(typed.replace(".", "").replace(",", "."))
        elif _DECIMAL_COMMA.fullmatch(typed):
            value = float(typed.replace(",", "."))
        else:
            return None
    except (ValueError, OverflowError):
        return None
    return value if math.isfinite(value) else None


class _Words:
    """The words of a text and how they join, worked out once. A chain is a row of words with only whitespace between them, and `ends[i]` is the last word of the chain holding word i."""

    def __init__(self, text: str, pattern: re.Pattern) -> None:
        self.spans = [(found.start(), found.end()) for found in pattern.finditer(text)]
        self.starts = [start for start, _ in self.spans]
        count = len(self.spans)
        self.linked = [False] * count  # word i+1 follows word i in the same chain
        for index in range(count - 1):
            self.linked[index] = text[self.spans[index][1]:self.spans[index + 1][0]].isspace()
        self.ends = list(range(count))
        self.next_join = [None] * count  # the first word at or after i, in its chain, that joins two phrases
        for index in range(count - 1, -1, -1):
            if index + 1 < count and self.linked[index]:
                self.ends[index] = self.ends[index + 1]
            start, end = self.spans[index]
            joins = text[start:end].lower() in CONNECTORS and self.linked[index]
            self.next_join[index] = index if joins else (self.next_join[index + 1] if index + 1 < count and self.linked[index] else None)

    def containing(self, position: int) -> int | None:
        index = bisect.bisect_right(self.starts, position) - 1
        if index >= 0 and self.spans[index][0] <= position < self.spans[index][1]:
            return index
        return None

    def split(self, text: str, first: int, start: int) -> tuple[str, str] | None:
        """The phrases on either side of the first joining word after word `first`, whose phrase starts at `start`."""
        if not self.linked[first]:
            return None
        join = self.next_join[first + 1]
        if join is None:
            return None
        return text[start:self.spans[join - 1][1]], text[self.spans[join + 1][0]:self.spans[self.ends[join]][1]]


def currency_sentence(text: str) -> Asked | None:
    """An amount, a phrase, then to, in or into, then another phrase, as in "how much is 20 bucks in euros". The joining word stands on
    its own between spaces, and the second phrase runs to the end of the letters, digits, spaces and dots that follow it."""
    found_amounts = amounts(text)
    if not found_amounts:
        return None
    words = _Words(text, _PHRASE_WORD)
    for found in found_amounts:
        after = found.end()
        if not text[after:after + 1].isspace():
            continue
        first_letter = _NOT_SPACE.search(text, after)
        if first_letter is None:
            continue
        first = words.containing(first_letter.start())
        if first is None or words.spans[first][0] != first_letter.start():
            continue
        phrases = words.split(text, first, first_letter.start())
        if phrases is not None:
            return Asked(found.group(), phrases[0].strip(), phrases[1].strip())
    return None


def currency_codes(text: str) -> Asked | None:
    """An amount followed by two currency codes, as in "100 usd eur" or "100USD to EUR"."""
    for found in amounts(text):
        codes = _CODES.match(text, found.end())
        if codes is not None:
            return Asked(found.group(), codes.group(1), codes.group(2))
    return None


def unit_sentence(text: str) -> Asked | None:
    """An amount, a unit of one or more words, then to, in or into, then another unit, as in "convert 5 km to miles" or "350F in C". The
    first word of the first unit can itself be "in", so "5 in to cm" works."""
    found_amounts = amounts(text)
    if not found_amounts:
        return None
    words = _Words(text, _UNIT_WORD)
    for found in found_amounts:
        start = found.end()
        while start < len(text) and text[start].isspace():
            start += 1
        first = words.containing(start)
        if first is None:
            continue
        phrases = words.split(text, first, start)
        if phrases is not None:
            return Asked(found.group(), phrases[0], phrases[1])
    return None
