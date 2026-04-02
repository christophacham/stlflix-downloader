# StLFlix Download Automation - Project Goals

## Overview
Automate the downloading of all downloadable content from StLFlix platform for a full subscription holder.

## Target Website
- **Base URL**: https://platform.stlflix.com
- **Authentication**: Username/password login required
- **Session Management**: Maintain authenticated session for downloads

## Platform Structure

### Drops Page
- URL pattern: `https://platform.stlflix.com/drops/{drop_number}`
- Example: https://platform.stlflix.com/drops/drop-315

### Product Pages
- Each drop contains multiple products
- URL pattern: `https://platform.stlflix.com/product/{slug}`
- Example: https://platform.stlflix.com/product/skull-sphere-table-lamp

### Downloadable Content
Products contain a "Download Files" section with multiple file types:
- **PDF** documents
- **FILES** (general downloadable files)
- **SOCIAL** Media Packages

## Download Objectives

### Directory Structure
```
stlflix/
├── drop-1/
│   └── {product-slug}/
│       ├── {original-filename}-drop315-productname.ext
│       └── ...
├── drop-2/
│   └── {product-slug}/
│       └── ...
...
└── drop-{N}/  (where N is the latest drop number)
    └── {product-slug}/
        └── ...
```

### File Naming Convention
- **Original name preserved**: Keep the source filename intact
- **Differentiator suffix**: Add unique identifier to prevent duplicates
  - Example: `file.pdf` → `file-drop315-skull-sphere-lamp.pdf`
- **Differentiators to consider**:
  - Drop number
  - Product slug/name
  - Index counter (if same file appears in multiple products)

### Requirements
1. **Authenticate** using username/password credentials
2. **Discover** all available drops (find the highest drop number)
3. **Iterate** through each drop sequentially
4. **Extract** product links from each drop page
5. **Navigate** to each product page
6. **Download** all files from the Download Files section
7. **Organize** into the structured directory hierarchy
8. **Log** all downloaded files for review

## Technical Considerations

### Authentication Flow
- Need to handle login form submission
- Maintain cookies/sessions across requests
- Handle potential 2FA or CAPTCHA challenges

### Discovery
- Find maximum drop number automatically
- Handle gaps in drop numbering if any exist

### File Deduplication
- Detect same filenames across products
- Add differentiators intelligently
- Log duplicate scenarios for manual review

### Error Handling
- Skip inaccessible products
- Log failed downloads separately
- Handle rate limiting gracefully

## Next Steps
1. [ ] Analyze the actual HTML structure of the pages
2. [ ] Determine the login mechanism (form fields, endpoints)
3. [ ] Identify how download files are embedded (direct links, dynamic loading)
4. [ ] Choose appropriate technology (Python with requests/BeautifulSoup or Playwright)
5. [ ] Build proof of concept for a single drop/product
6. [ ] Implement full automation
7. [ ] Add logging and progress tracking

## Files to Create
- `PROJECT_GOALS.md` (this file) - Project documentation
- `config.py` or `.env` - Store credentials securely
- `main.py` or `downloader.py` - Main automation script
- `requirements.txt` - Python dependencies
- `README.md` - Usage instructions
