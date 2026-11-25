"""Write rekordbox probe files: silent MP3, FLAC and AIFF with every candidate tag.

Text tags hold their own name. Numeric and date tags hold a distinct value,
listed in the printed legend. Import the files into rekordbox and read off
which tag each column shows.

Usage: python3 scripts/rekordbox_probe.py OUT_DIR
"""

from __future__ import annotations

import sys
from pathlib import Path

import mutagen.aiff
import mutagen.flac
import mutagen.id3
import mutagen.mp3
import numpy as np
import soundfile as sf

RATE = 44100
SECONDS = 10

# Numeric and date tags cannot show their own name, so each gets a value that
# identifies it.
ID3_TEXT = ["TPUB", "TPE2", "TPE4", "TIT3", "TALB"]
ID3_VALUES = {
    "TPOS": "7",
    "TDRC": "2001-01-01",
    "TDRL": "2002-02-02",
    "TDOR": "2003-03-03",
}
FLAC_TEXT = [
    "organization",
    "label",
    "publisher",
    "catalognumber",
    "albumartist",
    "album artist",
    "remixer",
    "subtitle",
    "version",
    "comment",
    "description",
    "album",
]
FLAC_VALUES = {"discnumber": "8", "date": "2004-04-04"}


def write_id3(id3: mutagen.id3.ID3, fmt: str) -> list[tuple[str, str]]:
    written = [("TIT2", f"Probe {fmt}"), ("TPE1", "crater probe")]
    written += [(name, name) for name in ID3_TEXT]
    written += ID3_VALUES.items()
    for name, val in written:
        id3[name] = getattr(mutagen.id3, name)(encoding=3, text=[val])
    id3["TXXX:CATALOGNUMBER"] = mutagen.id3.TXXX(
        encoding=3, desc="CATALOGNUMBER", text=["TXXX:CATALOGNUMBER"]
    )
    id3["COMM::eng"] = mutagen.id3.COMM(encoding=3, lang="eng", desc="", text=["COMM"])
    return written + [("TXXX:CATALOGNUMBER", "TXXX:CATALOGNUMBER"), ("COMM", "COMM")]


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    out = Path(sys.argv[1])
    out.mkdir(parents=True, exist_ok=True)
    silence = np.zeros((RATE * SECONDS, 2), dtype=np.float32)
    legend: list[tuple[str, str, str]] = []

    mp3 = out / "probe.mp3"
    sf.write(str(mp3), silence, RATE, format="MP3")
    audio = mutagen.mp3.MP3(mp3)
    if audio.tags is None:
        audio.add_tags()
    legend += [(mp3.name, k, v) for k, v in write_id3(audio.tags, "MP3")]
    audio.save()

    aiff = out / "probe.aiff"
    sf.write(str(aiff), silence, RATE, format="AIFF", subtype="PCM_16")
    audio = mutagen.aiff.AIFF(aiff)
    if audio.tags is None:
        audio.add_tags()
    legend += [(aiff.name, k, v) for k, v in write_id3(audio.tags, "AIFF")]
    audio.save()

    flac = out / "probe.flac"
    sf.write(str(flac), silence, RATE, format="FLAC")
    audio = mutagen.flac.FLAC(flac)
    if audio.tags is None:
        audio.add_tags()
    written = [("title", "Probe FLAC"), ("artist", "crater probe")]
    written += [(name, name) for name in FLAC_TEXT]
    written += FLAC_VALUES.items()
    for name, val in written:
        audio.tags[name] = [val]
    audio.save()
    legend += [(flac.name, k, v) for k, v in written]

    for file, tag, val in legend:
        print(f"{file:<12} {tag:<20} {val}")


if __name__ == "__main__":
    main()
