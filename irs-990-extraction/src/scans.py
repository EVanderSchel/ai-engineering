"""Simulated scans (step 5b): copies of the gold-set returns that look like paper run through a scanner.

Paper Form 990s are older than mandatory e-filing (tax years from mid-2020 on), so they have no e-file
XML and no answer keys. Instead, the e-filed returns' page images are degraded the way paper and a
scanner degrade a page: scanned at a lower resolution, faded ink on off-white paper, a slightly tilted
sheet, softened focus, speckles of dust, and JPEG compression. The words and numbers don't change, so
the answer keys still apply, and accuracy can be measured as the damage gets worse.

A scanned copy is an image PDF, like the IRS's own, so every stage reads it with no changes: the
extraction scripts' --scan option just points them at data/raw/scans/<level>/ instead of data/raw/pdf/.

What this doesn't simulate: handwriting, typewriters, other fonts and layouts, stamps, staples, and
pages out of order, all of which real paper returns have. It measures image quality alone.

    python src/scans.py               # make light, medium, and heavy copies of the dev returns
    python src/scans.py --level heavy --split test
"""

import argparse
import csv
import io
import random
import zlib
from dataclasses import dataclass

import pymupdf
from PIL import Image, ImageFilter

from paths import GOLD_DIR, RAW_DIR


@dataclass(frozen=True)
class Scan:
    dpi: int  # scanning resolution (the IRS's own images are 300 dpi)
    paper: int  # gray level of the paper, 255 = white
    ink: float  # how much of the ink's darkness survives, 1 = all of it
    tilt: float  # largest rotation of the sheet, in degrees, either way
    blur: float  # Gaussian blur radius, in pixels
    speckle: float  # share of pixels hit by dust, dark or light
    jpeg: int  # JPEG quality, 100 = best


LEVELS = {
    "light": Scan(dpi=200, paper=248, ink=0.95, tilt=0.5, blur=0.3, speckle=0.0003, jpeg=85),
    "medium": Scan(dpi=150, paper=238, ink=0.85, tilt=1.5, blur=0.7, speckle=0.0015, jpeg=60),
    "heavy": Scan(dpi=100, paper=225, ink=0.70, tilt=3.0, blur=1.0, speckle=0.004, jpeg=35),
}


def scan_dir(level: str):
    return RAW_DIR / "scans" / level


def source_pdf(object_id: str, scan: str | None = None):
    """The PDF a stage reads: the IRS's own, or a scanned copy made by this script."""
    path = RAW_DIR / "pdf" / f"{object_id}.pdf" if scan is None else scan_dir(scan) / f"{object_id}.pdf"
    if not path.exists():
        fix = "python src/build_gold.py" if scan is None else f"python src/scans.py --level {scan}"
        raise SystemExit(f"{path} is missing: run `{fix}`")
    return path


def degrade(image: Image.Image, scan: Scan, rng: random.Random) -> Image.Image:
    """One page image, made to look scanned. Grayscale in, grayscale out."""
    # Faded ink on off-white paper: white maps to the paper's gray, black to a lighter black.
    black = round(scan.paper * (1 - scan.ink))
    image = image.point(lambda v: black + (scan.paper - black) * v // 255)
    # A sheet that went in slightly crooked; the uncovered corners are paper-colored.
    image = image.rotate(rng.uniform(-scan.tilt, scan.tilt), resample=Image.Resampling.BICUBIC, fillcolor=scan.paper)
    image = image.filter(ImageFilter.GaussianBlur(scan.blur))
    # Dust: scattered dark and light specks.
    pixels = image.load()
    for _ in range(int(image.width * image.height * scan.speckle)):
        x, y = rng.randrange(image.width), rng.randrange(image.height)
        pixels[x, y] = rng.choice((black, black + 30, scan.paper, 255))
    # Saved as a JPEG, the way scanners usually do: blocky artifacts around text at low quality.
    buffer = io.BytesIO()
    image.save(buffer, "JPEG", quality=scan.jpeg)
    return Image.open(io.BytesIO(buffer.getvalue()))


def _seed(object_id: str, level: str) -> int:
    """The same return at the same level always gets the same damage."""
    return zlib.crc32(f"{object_id}:{level}".encode())


def make_scan(pdf_in, pdf_out, level: str, object_id: str) -> None:
    """Write a scanned copy of pdf_in: every page replaced by a degraded image of itself, same page size."""
    scan = LEVELS[level]
    rng = random.Random(_seed(object_id, level))
    with pymupdf.open(pdf_in) as source, pymupdf.open() as out:
        for page in source:
            pix = page.get_pixmap(dpi=scan.dpi, colorspace=pymupdf.csGRAY)
            image = Image.frombytes("L", (pix.width, pix.height), pix.samples)
            buffer = io.BytesIO()
            degrade(image, scan, rng).save(buffer, "JPEG", quality=scan.jpeg)
            new = out.new_page(width=page.rect.width, height=page.rect.height)
            new.insert_image(new.rect, stream=buffer.getvalue())
        pdf_out.parent.mkdir(parents=True, exist_ok=True)
        out.save(pdf_out, garbage=3, deflate=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--level", choices=sorted(LEVELS), action="append", help="default: all three")
    parser.add_argument("--split", choices=["dev", "test"], default="dev")
    args = parser.parse_args()

    with (GOLD_DIR / "manifest.csv").open(encoding="utf-8", newline="") as f:
        object_ids = [row["object_id"] for row in csv.DictReader(f) if row["split"] == args.split]
    for level in args.level or list(LEVELS):
        for object_id in object_ids:
            out = scan_dir(level) / f"{object_id}.pdf"
            if not out.exists():
                make_scan(RAW_DIR / "pdf" / f"{object_id}.pdf", out, level, object_id)
        print(f"{level}: {len(object_ids)} scanned returns in {scan_dir(level)}")


if __name__ == "__main__":
    main()
