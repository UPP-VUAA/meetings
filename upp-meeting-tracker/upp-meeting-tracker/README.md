# UPP Meeting Tracker

**Urban Phoenix Project / Valley Urban Action Alliance**

Automatically pulls Phoenix-area policy meetings every Friday and publishes them as a public webpage and Mailchimp-ready digest. Fully free. No paid services required.

---

## What it does

Every Friday at 7:00 AM Phoenix time, a GitHub Actions job runs the scraper, which:

1. Pulls upcoming meetings from **City of Phoenix** (Legistar API), **Valley Metro**, **MAG**, and **Maricopa County**
2. Scans agenda PDFs for keywords related to housing, zoning, transit, walkability, trees, heat mitigation, Vision Zero, building code, parking reform, and more
3. Publishes three output files to `docs/`:
   - `index.html` — the live public webpage (auto-served by GitHub Pages)
   - `digest.html` — a Mailchimp-paste-ready HTML block
   - `digest.txt` — a plain text version
   - `meetings.json` — structured data (for future integrations)

---

## One-time setup (takes about 20 minutes)

### Step 1 — Create the GitHub repository

1. Go to [github.com](https://github.com) and sign in (or create a free account — use your UPP email)
2. Click the **+** in the top right → **New repository**
3. Name it: `meetings` (or `meeting-tracker`)
4. Set it to **Public** (required for free GitHub Pages)
5. Click **Create repository**
6. Upload all the files from this folder into the repository root:
   - `scraper.py`
   - `requirements.txt`
   - `.github/workflows/weekly-tracker.yml`
   - `docs/index.html`
   - `docs/.nojekyll`
   - `README.md`

   The easiest way: drag and drop them into the GitHub web interface, or use GitHub Desktop if you prefer a visual tool.

### Step 2 — Enable GitHub Pages

1. In your repository, click **Settings** (top tab)
2. In the left sidebar, click **Pages**
3. Under **Source**, select **Deploy from a branch**
4. Under **Branch**, select `main` and set the folder to `/docs`
5. Click **Save**
6. Wait 1-2 minutes. Your site will be live at:
   `https://[your-github-username].github.io/meetings`

   For example: `https://urbanphoenixproject.github.io/meetings`

### Step 3 — Test the scraper manually

Before waiting for Friday, run it once now to confirm it works:

1. In your repository, click the **Actions** tab
2. Click **Weekly Meeting Tracker** in the left sidebar
3. Click **Run workflow** → **Run workflow** (green button)
4. Wait about 2 minutes. A green checkmark means success.
5. Refresh your GitHub Pages URL — you should see meetings populated.

If you see a red X, click on it to see the error log. Common issues are listed in the Troubleshooting section below.

### Step 4 — Set your Mailchimp workflow

Every Friday after 7 AM (or whenever the job runs), do this:

1. Go to your repository → `docs/digest.html`
2. Click the file, then click the **Raw** button
3. Select all (Ctrl+A), copy
4. In Mailchimp, create a new campaign
5. In the email editor, add an **HTML** content block
6. Paste the copied HTML into it
7. Adjust your subject line, header, and footer as needed, then send

Total time: about 10 minutes per week.

**Optional automation:** If you want Mailchimp to send fully automatically (zero touch), Mailchimp's free plan supports RSS-to-email campaigns. You can point it at the `meetings.json` file with a custom template, but this requires some additional setup. Come back to this once the basics are running.

---

## Customization

### Adding or removing keywords

Open `scraper.py` and find the `KEYWORDS` list near the top. Add any term you want scanned in agenda PDFs. Changes take effect on the next run.

### Adding a meeting body

**For City of Phoenix Legistar bodies** (City Council, subcommittees, Planning Commission):
Find `PHOENIX_LEGISTAR_BODIES` in `scraper.py` and add the exact body name as it appears on [phoenix.legistar.com/Calendar.aspx](https://phoenix.legistar.com/Calendar.aspx).

**For Phoenix boards/commissions** (Village Planning Committees, DAB, etc.):
Find `PHOENIX_BOARDS_KEYWORDS` and add a keyword that appears in the meeting notice name.

### Changing the look-ahead window

Find `LOOK_AHEAD_DAYS = 28` near the top of `scraper.py` and change the number. Default is 28 days (4 weeks).

### Embedding on your UPP website

Add this iframe anywhere on your site:

```html
<iframe
  src="https://[your-github-username].github.io/meetings"
  width="100%"
  height="800"
  frameborder="0"
  style="border: none; border-radius: 8px;">
</iframe>
```

Or link directly to the page with a button or text link.

---

## Sources covered

| Agency | What's tracked |
|---|---|
| City of Phoenix — Legistar | City Council Formal, Policy Sessions, Special Meetings, all active subcommittees (Transportation/Infrastructure/Planning; Community Services & Education; Public Safety & Justice; Economic Development & Arts), Planning Commission, Budget Hearings |
| City of Phoenix — Boards & Commissions | Development Advisory Board, DAB Subcommittee, Environmental Quality & Sustainability Commission, Village Planning Committees (all 15), Citizen Transportation Commission, Vision Zero Advisory Board, Mayor's Commission on Disability Issues |
| Valley Metro | Board of Directors (RPTA & VMR), TMC/RMC meetings |
| MAG | Regional Council, RPCC, Transportation Policy Committee, Environment & Sustainable Communities Committee |
| Maricopa County | Board of Supervisors (Formal & Informal), Planning & Zoning hearings |

---

## Keyword list (current)

Housing, affordable housing, zoning, rezoning, general plan, land use, density, multifamily, ADU, middle housing, building code, construction code, single stair, transit, light rail, BRT, walkability, pedestrian, sidewalk, sidewalks, crosswalk, crosswalks, bike lane, bike lanes, bicycle, vision zero, street safety, road diet, road enhancement, traffic calming, speed limit, speed camera, parking, parking reform, heat, heat mitigation, urban heat, tree, trees, canopy, tree protection, shade, climate, sustainability, environment, budget, capital improvement, public hearing.

---

## Troubleshooting

**The Actions job fails with a permissions error**
Go to Settings → Actions → General → Workflow permissions → select "Read and write permissions" → Save.

**No meetings are showing up**
This is usually a rate limit or block from a source. Run the job again in a few hours. The MAG calendar sometimes blocks automated requests — a placeholder link is inserted in that case.

**The webpage looks broken**
Check that the `docs/` folder exists in your repository and that GitHub Pages is set to serve from `/docs`.

**A specific meeting body is missing**
Check the exact name on the source agency's website and add it to the appropriate list in `scraper.py`.

---

## File structure

```
upp-meeting-tracker/
├── scraper.py                         # main script
├── requirements.txt                   # Python dependencies
├── README.md                          # this file
├── .github/
│   └── workflows/
│       └── weekly-tracker.yml         # GitHub Actions schedule
└── docs/                              # GitHub Pages output folder
    ├── index.html                     # live public webpage (auto-generated)
    ├── digest.html                    # Mailchimp HTML block (auto-generated)
    ├── digest.txt                     # plain text digest (auto-generated)
    ├── meetings.json                  # structured data (auto-generated)
    └── .nojekyll                      # tells GitHub Pages to skip Jekyll processing
```

---

*Built for Urban Phoenix Project and Valley Urban Action Alliance. Free forever.*
