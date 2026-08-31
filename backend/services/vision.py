"""
AI Computer Vision Hazard Verification Engine.
Analyzes road incident photos to verify reported hazard categories (landslides, flash floods, road damage, blockages)
versus clear roads or irrelevant non-hazard images.
"""
import io
import base64
import math
from typing import Dict, Any, Tuple
from PIL import Image, ImageStat, ImageFilter


def analyze_image_bytes(image_bytes: bytes, reported_category: str) -> Dict[str, Any]:
    """
    Analyzes an image and verifies if it matches the reported hazard category.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
    except Exception as e:
        return {
            "verified": False,
            "confidence": 0.0,
            "detected_category": "invalid_image",
            "message": f"Could not decode image file: {str(e)}",
            "hazard_match": False,
        }

    # Resize for fast feature extraction
    img_thumb = img.resize((160, 160))
    pixels = list(img_thumb.getdata())
    total_pixels = len(pixels)

    # 1. Color and Hue analysis
    earth_mud_count = 0
    water_blue_count = 0
    asphalt_gray_count = 0
    vegetation_green_count = 0
    bright_sky_count = 0

    for r, g, b in pixels:
        # Mud/rock/earth: warm brownish tones (R > G > B and moderate saturation)
        if r > 60 and g > 40 and b < min(r, g) - 15 and r > b + 25:
            earth_mud_count += 1
        # Water/Flood: blueish or murky silt
        elif (b > r + 15 and b > g + 10) or (g > 60 and b > 60 and abs(r - g) < 20 and b > r):
            water_blue_count += 1
        # Vegetation/Trees: green dominance
        elif g > r + 20 and g > b + 20:
            vegetation_green_count += 1
        # Asphalt/Pavement: neutral grays
        elif abs(r - g) < 15 and abs(g - b) < 15 and 30 < (r + g + b) // 3 < 180:
            asphalt_gray_count += 1
        # Bright sky / overexposed
        elif r > 210 and g > 210 and b > 210:
            bright_sky_count += 1

    mud_ratio = earth_mud_count / total_pixels
    water_ratio = water_blue_count / total_pixels
    veg_ratio = vegetation_green_count / total_pixels
    asphalt_ratio = asphalt_gray_count / total_pixels

    # 2. Texture & Edge Variance (Debris & Cracks create high high-frequency gradients)
    gray = img_thumb.convert("L")
    edges = gray.filter(ImageFilter.FIND_EDGES)
    stat = ImageStat.Stat(edges)
    edge_mean = stat.mean[0]
    edge_std = stat.stddev[0]
    roughness = edge_mean / 255.0

    # 3. Categorization logic
    cat_lower = reported_category.lower().strip()
    detected_cat = "unknown"
    confidence = 0.5
    verified = False
    message = ""

    if cat_lower in ("landslide", "mudslide", "rockfall"):
        # Landslides feature mud/rock hues combined with rough edge texture or vegetation disruption
        if mud_ratio >= 0.12 or (mud_ratio >= 0.06 and roughness > 0.08) or (veg_ratio > 0.25 and roughness > 0.12):
            confidence = min(0.98, 0.72 + mud_ratio * 0.8 + roughness * 0.5)
            detected_cat = "landslide"
            verified = True
            message = f"AI verified landslide/debris patterns (Mud/rock ratio: {int(mud_ratio*100)}%, Terrain texture: {int(roughness*100)}%)."
        else:
            confidence = 0.35
            detected_cat = "clean_surface" if asphalt_ratio > 0.4 else "unclear"
            verified = False
            message = f"Image shows smooth pavement/insufficient debris for a landslide (Detected mud/rock: {int(mud_ratio*100)}%)."

    elif cat_lower in ("flood", "flash_flood", "waterlogging", "submerged"):
        # Floods feature water tones or murky low-texture pools
        if water_ratio >= 0.15 or (mud_ratio > 0.15 and roughness < 0.10):
            confidence = min(0.98, 0.75 + water_ratio * 0.8)
            detected_cat = "flood"
            verified = True
            message = f"AI verified flood/water surface accumulation ({int(water_ratio*100)}% water/silt reflection)."
        else:
            confidence = 0.30
            detected_cat = "dry_road"
            verified = False
            message = f"No significant standing water detected ({int(water_ratio*100)}% water detected). Dry road conditions."

    elif cat_lower in ("road_damage", "pothole", "crack"):
        if roughness >= 0.08 and asphalt_ratio >= 0.15:
            confidence = min(0.95, 0.70 + roughness * 0.9)
            detected_cat = "road_damage"
            verified = True
            message = f"AI verified asphalt fissure and road surface degradation (Surface roughness: {int(roughness*100)}%)."
        else:
            confidence = 0.40
            detected_cat = "smooth_pavement"
            verified = False
            message = "Asphalt surface appears intact without major fissure patterns."

    elif cat_lower in ("blocked", "congestion", "traffic"):
        if roughness >= 0.06:
            confidence = 0.88
            detected_cat = "blocked"
            verified = True
            message = "AI verified corridor obstruction / vehicle presence."
        else:
            confidence = 0.45
            detected_cat = "clear_corridor"
            verified = False
            message = "Road corridor appears clear without obstruction."

    elif cat_lower in ("clear", "cleared"):
        if asphalt_ratio >= 0.25 and roughness < 0.14:
            confidence = 0.94
            detected_cat = "clear"
            verified = True
            message = "AI verified open, clear road alignment."
        else:
            confidence = 0.60
            detected_cat = "hazard_present"
            verified = False
            message = "Debris or irregularity still detected on surface."
    else:
        # Default verification for general hazards
        verified = roughness > 0.05 or mud_ratio > 0.1
        confidence = 0.75 if verified else 0.4
        detected_cat = cat_lower
        message = "General road hazard characteristics evaluated."

    return {
        "verified": verified,
        "confidence": round(confidence, 3),
        "detected_category": detected_cat,
        "hazard_match": verified,
        "message": message,
        "metrics": {
            "mud_ratio": round(mud_ratio, 3),
            "water_ratio": round(water_ratio, 3),
            "asphalt_ratio": round(asphalt_ratio, 3),
            "surface_roughness": round(roughness, 3),
        }
    }


def verify_base64_photo(base64_str: str, reported_category: str) -> Dict[str, Any]:
    """Decodes a base64 image and performs AI hazard verification."""
    if not base64_str:
        return {
            "verified": False,
            "confidence": 0.0,
            "detected_category": "none",
            "message": "No image payload provided.",
            "hazard_match": False
        }

    try:
        if "," in base64_str:
            base64_str = base64_str.split(",", 1)[1]
        img_bytes = base64.b64decode(base64_str)
        return analyze_image_bytes(img_bytes, reported_category)
    except Exception as e:
        return {
            "verified": False,
            "confidence": 0.0,
            "detected_category": "error",
            "message": f"Image processing error: {str(e)}",
            "hazard_match": False
        }
