# Branding

Home Assistant brand assets for this custom integration live in:

```text
custom_components/ebay/brand/
```

Expected files:

```text
icon.png        256x256 square PNG
icon@2x.png     512x512 square PNG
logo.png        landscape PNG, shortest side 128-256 px
logo@2x.png     landscape PNG, shortest side 256-512 px
```

Generate the files from the Wikimedia eBay logo:

```bash
python -m pip install pillow
python scripts/prepare_brand_assets.py
```

The README uses the same image as a remote image:

```markdown
<p align="center">
  <img src="https://upload.wikimedia.org/wikipedia/commons/4/48/EBay_logo.png" alt="eBay logo" width="220">
</p>
```

Trademark notice:

```markdown
eBay is a trademark of eBay Inc. This project is an unofficial Home Assistant integration and is not affiliated with, endorsed by, or sponsored by eBay Inc. The eBay logo is used only for identification.
```

Notes:

- PNG only.
- Prefer transparent backgrounds.
- Icon must be square.
- Normal icon size is 256x256.
- HiDPI icon size is 512x512.
- Logos should be landscape and preserve the brand aspect ratio.
