#!/usr/bin/env python3
import os
import sys
import base64
import subprocess
from PIL import Image, ImageDraw, ImageFont

def draw_ghost(size):
    # Create mask image for the ghost body
    mask = Image.new("L", (size, size), 0)
    draw_mask = ImageDraw.Draw(mask)
    
    # Scale variables
    pad = size * 0.15
    w = size - 2 * pad
    h = size - 2 * pad
    
    # Head circle on mask
    draw_mask.ellipse([pad, pad, pad + w, pad + h * 0.75], fill=255)
    
    # Body rect on mask
    draw_mask.rectangle([pad, pad + h * 0.375, pad + w, pad + h * 0.85], fill=255)
    
    # Tail waves on mask
    wave_r = w / 6
    draw_mask.ellipse([pad, pad + h * 0.75, pad + wave_r * 2, pad + h * 0.95], fill=255)
    draw_mask.ellipse([pad + wave_r * 2, pad + h * 0.75, pad + wave_r * 4, pad + h * 0.95], fill=255)
    draw_mask.ellipse([pad + wave_r * 4, pad + h * 0.75, pad + w, pad + h * 0.95], fill=255)
    
    # Create the gradient image
    gradient = Image.new("RGBA", (size, size))
    # Start: (124, 58, 237) -> End: (16, 185, 129)
    for y in range(size):
        factor = y / float(size - 1) if size > 1 else 0
        r = int(124 + (16 - 124) * factor)
        g = int(58 + (185 - 58) * factor)
        b = int(237 + (129 - 237) * factor)
        for x in range(size):
            gradient.putpixel((x, y), (r, g, b, 255))
            
    # Composite gradient using the ghost mask
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    img.paste(gradient, (0, 0), mask)
    
    # Draw eyes and mouth on top of the ghost
    draw = ImageDraw.Draw(img)
    
    # Eyes (dark)
    eye_size = w * 0.14
    draw.ellipse([pad + w * 0.22, pad + h * 0.3, pad + w * 0.22 + eye_size, pad + h * 0.3 + eye_size], fill=(10, 10, 15, 255))
    draw.ellipse([pad + w * 0.63, pad + h * 0.3, pad + w * 0.63 + eye_size, pad + h * 0.3 + eye_size], fill=(10, 10, 15, 255))
    
    # Mouth (smile)
    draw.arc([pad + w * 0.35, pad + h * 0.42, pad + w * 0.65, pad + h * 0.62], start=0, end=180, fill=(10, 10, 15, 255), width=max(1, int(size * 0.03)))
    
    return img

def get_font(font_size):
    paths = [
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/TTF/DejaVuSans-Bold.ttf",
        "/usr/share/fonts/truetype/freefont/FreeSansBold.ttf",
        "/usr/share/fonts/liberation/LiberationSans-Bold.ttf"
    ]
    for p in paths:
        if os.path.exists(p):
            try:
                return ImageFont.truetype(p, font_size)
            except Exception:
                continue
    return ImageFont.load_default()

def main():
    print("Generating SVG logo...")
    svg_content = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 512 512" width="100%" height="100%">
  <defs>
    <linearGradient id="ghostGrad" x1="0%" y1="0%" x2="100%" y2="100%">
      <stop offset="0%" stop-color="#7C3AED" />
      <stop offset="100%" stop-color="#10B981" />
    </linearGradient>
    <filter id="glow" x="-20%" y="-20%" width="140%" height="140%">
      <feGaussianBlur stdDeviation="20" result="blur" />
      <feComposite in="SourceGraphic" in2="blur" operator="over" />
    </filter>
  </defs>
  <circle cx="256" cy="256" r="220" fill="url(#ghostGrad)" opacity="0.15" filter="url(#glow)" />
  <g fill="url(#ghostGrad)">
    <ellipse cx="256" cy="211.2" rx="179.2" ry="134.4" />
    <rect x="76.8" y="211.2" width="358.4" height="170.24" />
    <ellipse cx="136.53" cy="381.44" rx="59.73" ry="35.84" />
    <ellipse cx="256" cy="381.44" rx="59.73" ry="35.84" />
    <ellipse cx="375.47" cy="381.44" rx="59.73" ry="35.84" />
  </g>
  <circle cx="180.7" cy="209.4" r="25" fill="#0a0a0f" />
  <circle cx="327.7" cy="209.4" r="25" fill="#0a0a0f" />
  <path d="M202.2,263.2 Q256,299 309.8,263.2" stroke="#0a0a0f" stroke-width="15" stroke-linecap="round" fill="none" />
</svg>
"""
    os.makedirs("static", exist_ok=True)
    with open("static/logo.svg", "w") as f:
        f.write(svg_content)
        
    print("Generating PNG logo files...")
    sizes = [32, 64, 128, 512]
    for size in sizes:
        img = draw_ghost(size)
        img.save(f"static/logo_{size}.png", "PNG")
        print(f"  Created static/logo_{size}.png")

    print("Generating logo banner for video encoding...")
    # Create a 640x360 dark banner
    banner = Image.new("RGBA", (640, 360), (10, 10, 15, 255))
    draw = ImageDraw.Draw(banner)
    
    # Draw a subtle background mesh/glow
    for y in range(0, 360, 40):
        draw.line([(0, y), (640, y)], fill=(255, 255, 255, 4))
    for x in range(0, 640, 40):
        draw.line([(x, 0), (x, 360)], fill=(255, 255, 255, 4))
        
    # Draw ghost logo on the banner (size 120x120, centered top-ish)
    ghost_img = draw_ghost(120)
    banner.alpha_composite(ghost_img, (260, 80))
    
    # Write text "Media removed with MediaSpektor"
    text = "Media removed with MediaSpektor"
    font = get_font(24)
    # Get text size
    if hasattr(draw, "textbbox"):
        bbox = draw.textbbox((0, 0), text, font=font)
        text_w = bbox[2] - bbox[0]
        text_h = bbox[3] - bbox[1]
    else:
        text_w, text_h = draw.textsize(text, font=font)
        
    draw.text(((640 - text_w) // 2, 230), text, fill=(255, 255, 255, 220), font=font)
    banner.convert("RGB").save("scratch/logo_banner.png", "PNG")
    print("  Created scratch/logo_banner.png")
    
    print("Compiling videos using ffmpeg...")
    os.makedirs("scratch/videos", exist_ok=True)
    
    # Generate MP4
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", "scratch/logo_banner.png",
        "-c:v", "libx264", "-t", "1", "-pix_fmt", "yuv420p",
        "-vf", "scale=640:360", "-profile:v", "baseline", "-level", "3.0",
        "-an", "scratch/videos/dummy.mp4"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Generate MKV
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", "scratch/logo_banner.png",
        "-c:v", "libx264", "-t", "1", "-pix_fmt", "yuv420p",
        "-vf", "scale=640:360", "-an", "scratch/videos/dummy.mkv"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    # Generate AVI
    subprocess.run([
        "ffmpeg", "-y", "-loop", "1", "-i", "scratch/logo_banner.png",
        "-c:v", "mpeg4", "-t", "1", "-vf", "scale=640:360",
        "-an", "scratch/videos/dummy.avi"
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    
    print("Video files created:")
    for ext in ["mp4", "mkv", "avi"]:
        path = f"scratch/videos/dummy.{ext}"
        if os.path.exists(path):
            size = os.path.getsize(path)
            print(f"  {path} ({size} bytes)")
            
            # Print base64 string
            with open(path, "rb") as vf:
                encoded = base64.b64encode(vf.read()).decode("utf-8")
                # Write to text file for easy insertion
                with open(f"scratch/videos/dummy_{ext}_base64.txt", "w") as bf:
                    bf.write(encoded)
        else:
            print(f"  Error: {path} not created!")

if __name__ == "__main__":
    main()
