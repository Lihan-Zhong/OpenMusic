"""Do-nothing floor: every stem is the full mixture. Any real method should beat this."""


def separate(mix, sr):
    return {"vocals": mix, "drums": mix, "other": mix}
