#!/usr/bin/env python3
"""Package a generated 3x2 sprite sheet as a six-frame GIF; no artwork synthesis."""
import argparse
from pathlib import Path
from PIL import Image


def pack(source, target, durations, selected=None):
    sheet = Image.open(source).convert('RGBA')
    width, height = sheet.size
    if width % 3 or height % 2:
        raise ValueError('Sheet must divide exactly into 3 columns and 2 rows')
    w, h = width // 3, height // 2
    # A single palette avoids palette drift between frames. GIF reserves index 0
    # for transparency; alpha threshold is only the GIF format conversion.
    palette = sheet.convert('RGB').quantize(colors=255)
    colors = palette.getpalette()[:765]
    frames = []
    cuts = []
    # Locate clear gutters near the requested cell divisions: generated scenes
    # may exceed a nominal boundary by a pixel. Preserve every drawn pixel.
    for row in range(2):
        alpha_row = sheet.getchannel('A').crop((0, row*h, width, (row+1)*h))
        row_cuts = [0]
        for nominal in (w, 2*w):
            candidates = sorted(range(nominal-64, nominal+65), key=lambda x: abs(x-nominal))
            clear = next((x for x in candidates if alpha_row.crop((x,0,x+1,h)).getextrema()[1] < 128), None)
            if clear is None:
                raise ValueError('No clear gutter near a cell division')
            row_cuts.append(clear)
        cuts.append(row_cuts+[width])
    selected = list(range(6)) if selected is None else selected
    for i in selected:
        col, row = i%3, i//3
        left, right = cuts[row][col:col+2]
        piece = sheet.crop((left, row*h, right, (row+1)*h))
        frame = Image.new('RGBA', (w+160,h))
        frame.paste(piece, (80+left-col*w,0))
        alpha = frame.getchannel('A').point(lambda a: 255 if a >= 128 else 0)
        box = alpha.getbbox()
        if not box or box[0] == 0 or box[1] == 0 or box[2] == w+160 or box[3] == h:
            raise ValueError(f'Frame {i} empty or touches a cell boundary: {box}')
        indexed = frame.convert('RGB').quantize(palette=palette, dither=Image.Dither.NONE)
        indexed = indexed.point(lambda p: p+1)
        indexed.putpalette([0,0,0]+colors)
        indexed.paste(0, mask=alpha.point(lambda a: 255-a))
        frames.append(indexed)
    Path(target).parent.mkdir(parents=True, exist_ok=True)
    frames[0].save(target, save_all=True, append_images=frames[1:], duration=durations,
                   loop=0, transparency=0, disposal=2, optimize=False)
    with Image.open(target) as gif:
        assert gif.n_frames == len(selected)
    print(f'{target}: {len(selected)} frames, {w+160}x{h}, transparent, looping')


if __name__ == '__main__':
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('source'); p.add_argument('target')
    p.add_argument('--durations', default='220,180,180,220,180,180')
    p.add_argument('--frames', default='0,1,2,3,4,5', help='Zero-based frame indices in playback order')
    args = p.parse_args()
    durations = [int(n) for n in args.durations.split(',')]
    selected = [int(n) for n in args.frames.split(',')]
    if len(durations) != len(selected) or min(durations) < 20 or any(n not in range(6) for n in selected):
        p.error('Provide one duration >=20 ms for each selected frame index (0–5)')
    pack(args.source, args.target, durations, selected)
