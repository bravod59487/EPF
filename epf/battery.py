"""Turning the frame's reported voltage into a percentage."""

# Lithium discharge curve: millivolts -> percent
LEVELS = {
    4200: 100,
    4150: 95,
    4110: 90,
    4080: 85,
    4020: 80,
    3980: 75,
    3950: 70,
    3910: 65,
    3870: 60,
    3850: 55,
    3840: 50,
    3820: 45,
    3800: 40,
    3790: 35,
    3770: 30,
    3750: 25,
    3730: 20,
    3710: 15,
    3690: 10,
    3610: 5,
    3400: 0,
}

def percentage(voltage):
    """
    Percentage for a voltage in millivolts, interpolated between the two nearest
    points on the curve rather than assuming it is a straight line.
    """
    if voltage >= 4200:
        return 100
    if voltage <= 3400:
        return 0

    voltages = list(LEVELS.keys())
    for i in range(len(voltages) - 1):
        upper, lower = voltages[i], voltages[i + 1]
        if upper >= voltage >= lower:
            high, low = LEVELS[upper], LEVELS[lower]
            return round(low + (voltage - lower) * (high - low) / (upper - lower), 1)

    return 0
