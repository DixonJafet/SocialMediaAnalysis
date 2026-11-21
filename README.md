# SocialMediaStats

**SocialMediaStats** is a Python and Power BI project that collects mirrored social media publications from Instagram and Facebook, stores the extracted data in MongoDB, and applies AI-based sentiment, tone, and topic classification.

The project was built as a portfolio project to explore browser automation, resumable data pipelines, MongoDB storage, Azure AI Foundry inference, and social media reporting.

---

## Overview

The pipeline identifies matching publications from two Costa Rican public accounts and processes the information shown on both networks:

- Instagram posts and reels
- Facebook mirror posts
- Publication captions and timestamps
- Up to 31 comments per publication
- Instagram likes
- Facebook reaction totals and types

The extracted records are then classified and prepared for analysis in a Power BI dashboard.

---

## Tech Stack

| Layer | Technology |
|-------|------------|
| **Language** | Python |
| **Browser Automation** | Playwright with Microsoft Edge |
| **Database** | MongoDB |
| **AI Classification** | Azure AI Foundry / GPT-5 mini |
| **Visualization** | Microsoft Power BI |
| **Testing** | Python `unittest` |

---

## Project Structure

| Path | Purpose |
|------|---------|
| `pipeline.py` | Runs the extraction and classification phases |
| `social_media/batch.py` | Discovers and processes social media publications |
| `social_media/scraper.py` | Reads Instagram and Facebook data |
| `social_media/storage.py` | Updates MongoDB records transactionally |
| `social_media/label_social_media.py` | Classifies captions and comments |
| `tests/` | Synthetic extraction and pipeline tests |
| `dashboard/SocialMediaDashboard.pbix` | Power BI dashboard |
| `docs/instrucciones.txt` | Original project requirements |

---

## Features

- Discovers Instagram publications and searches for matching Facebook posts
- Matches publications using the normalized caption and publication date
- Processes publications incrementally with resumable SQLite state
- Stores data in two MongoDB collections
- Collects up to 31 displayed comments from each network
- Groups Facebook reactions by type
- Labels comments with:
  - Positive, negative, or neutral sentiment
  - A single tone such as sarcasm, criticism, support, anger, or hope
- Categorizes publications by topic
- Preserves existing labels and publication content where appropriate
- Provides a Power BI dashboard for exploring the collected records

---

## Data Sources

The extractor uses these reference accounts:

| Instagram | Facebook | Starting date |
|-----------|----------|---------------|
| `laura_fernandez_delgado` | `laurafernandezdelgado2026` | February 3, 2026 |
| `presidenciacr` | `CasaPresidencial` | May 10, 2026 |

Facebook dates and captions are used to verify that an Instagram and Facebook publication are mirrors. The script does not download media or publish interactions.

---

## Requirements

- Python 3.10 or newer
- Microsoft Edge
- MongoDB with access to `SocialMediaDB`
- An existing Azure AI Foundry deployment
- Azure CLI access for model authentication
- Playwright browser support

---

## Quick Start

Create an environment and install the dependencies:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r .\requirements.txt
```

Sign in to Azure:

```powershell
az login
```

Create a `.env` file in the project root:

```env
MONGODB_URI=<your-mongodb-uri>
MONGODB_DB=SocialMediaDB
FOUNDRY_ENDPOINT=<your-foundry-endpoint>
FOUNDRY_DEPLOYMENT=gpt-5-mini-1
```

Launch Microsoft Edge with a persistent browser profile by opening a headed Playwright session during the first login:

```powershell
.\.venv\Scripts\python.exe -u .\pipeline.py --write --headed
```

Sign in manually to Instagram and Facebook in the browser profile. Keep credentials, MongoDB connection strings, and `.env` files outside source control.

---

## Running the Pipeline

Preview classification work without extracting, calling Azure, or writing to MongoDB:

```powershell
.\.venv\Scripts\python.exe .\pipeline.py
```

Run extraction followed by classification:

```powershell
.\.venv\Scripts\python.exe -u .\pipeline.py --write
```

Limit processing to a profile or a number of publications:

```powershell
.\.venv\Scripts\python.exe -u .\pipeline.py --write --account presidenciacr --max-posts 2 --headed
```

Useful extraction options include:

- `--skip-discovery` — reuse previously discovered publication URLs
- `--retry-errors` — retry extraction errors
- `--retry-unresolved` — retry unresolved mirrors
- `--headed` — show the browser window
- `--relabel` — reclassify the complete MongoDB collection

The write pipeline sends descriptions and comments to Azure and writes to MongoDB, so Azure usage charges and data privacy policies should be reviewed before execution.

---

## MongoDB Structure

The project uses an existing MongoDB database named `SocialMediaDB`:

- `SocialMediaDB.SocialMediaDB` contains the publication records
- `SocialMediaDBInfo.SocialMediaDB` contains Instagram/Facebook URL mappings and extraction metadata

Each publication stores:

- A shared caption
- Publication timestamp
- Instagram account, URL, likes, comments, and generated comment labels
- Facebook account, URL, reactions, comments, and generated comment labels
- A topic category

The existing `instagram_url` unique index is required before new records can be written.

---

## Power BI Dashboard

The dashboard is provided in:

```text
dashboard/SocialMediaDashboard.pbix
```

Open it with Microsoft Power BI Desktop and connect it to the MongoDB records or exported dataset for the required reporting environment.

---

## Testing

The test suite uses synthetic data and does not contact Azure or modify MongoDB:

```powershell
.\.venv\Scripts\python.exe -m unittest discover -s .\tests -p "test_*.py" -v
```

---

## Notes

- Mirror matching depends on visible dates and normalized captions
- Platform selectors and login flows can change
- Automatic classification can make mistakes, especially with sarcasm
- Unresolved matches do not prove that a mirror publication does not exist
- A human review of the classified dataset is recommended

---

🧠 *Developed by Jafet Dixon*