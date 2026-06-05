# Dashboard: APEC Projects

This repository contains an automated data pipeline and an interactive dashboard for exploring publicly available information from the APEC Project Database.

The project combines Python-based web scraping with a Quarto/Shiny dashboard. The scraper collects project-level information from the APEC Project Database, structures the results into clean tabular files, and exports the dataset in CSV and XLSX formats. The dashboard uses those files to provide an interactive interface for filtering, visualizing, and downloading the data.

**Live dashboard:** https://lfacostaz.shinyapps.io/dashboard-apec-projects/

## Project overview

The dataset includes approved APEC projects with variables such as project title, project number, year, sponsoring forum, proposing economy, project status, funding source, project value, co-sponsoring economies, organizations involved, and project links.

The objective is to make APEC project information easier to explore, compare, and analyze across time, economies, fora, committees, topics, and funding categories.

## Data pipeline

The data pipeline is built in Python. It scrapes approved APEC projects from the APEC Project Database by paginating through the list view and retrieving each project's detail page.

The scraper uses `requests` and `BeautifulSoup` for data collection, `ThreadPoolExecutor` for parallel processing, and `pandas` for cleaning, structuring, deduplication, and export.

The resulting datasets are saved in the `data/` folder:

```text
data/apec_approved_projects.csv
data/apec_approved_projects.xlsx
```

A copy of the Excel dataset is also saved in the `dashboard/` folder so the Shiny app can access the data during deployment:

```text
dashboard/apec_approved_projects.xlsx
```

## Dashboard

The dashboard is built with R, Quarto, and Shiny. It provides a multi-filter interface and several visualization tabs for exploring the data.

The sidebar allows filtering by year, economy, forum, committee, topic, project status, and fund type. The dashboard also includes KPI cards showing total projects, total project value, average project value, and the share of self-funded projects.

The visualization tabs cover project counts by year, forum, and economy; project value by year and funding source; trends over time by economy, forum, committee, and topic; and a searchable project-level data table. Filtered data can be exported directly to XLSX.

The dashboard source file and rendered outputs are stored in the `dashboard/` folder:

```text
dashboard/dashboard-apec-projects.qmd
dashboard/dashboard-apec-projects.html
dashboard/dashboard-apec-projects_files/
```

## Automation

The update process runs through GitHub Actions. The workflow executes the Python scraper, regenerates the datasets, renders the Quarto/Shiny dashboard, commits updated files to the repository, and redeploys the latest version to shinyapps.io.

## Repository structure

```text
dashboard-apec-projects/
├── .github/
│   └── workflows/
│       └── daily-update.yml
├── dashboard/
│   ├── apec_approved_projects.xlsx
│   ├── dashboard-apec-projects.qmd
│   ├── dashboard-apec-projects.html
│   └── dashboard-apec-projects_files/
├── data/
│   ├── apec_approved_projects.csv
│   └── apec_approved_projects.xlsx
├── script/
│   └── web-scraping-apec-pdb.py
├── .gitattributes
├── .gitignore
├── README.md
└── requirements.txt
```

## Main files

| Path                                     | Description                                               |
| ---------------------------------------- | --------------------------------------------------------- |
| `script/web-scraping-apec-pdb.py`        | Production Python scraper used by the automated workflow. |
| `data/apec_approved_projects.csv`        | Clean dataset in CSV format.                              |
| `data/apec_approved_projects.xlsx`       | Clean dataset in Excel format.                            |
| `dashboard/apec_approved_projects.xlsx`  | Excel copy used by the Shiny dashboard during deployment. |
| `dashboard/dashboard-apec-projects.qmd`  | Quarto/Shiny dashboard source file.                       |
| `dashboard/dashboard-apec-projects.html` | Rendered dashboard output.                                |
| `.github/workflows/daily-update.yml`     | GitHub Actions workflow for daily updates and deployment. |

## Requirements

Python dependencies are listed in:

```text
requirements.txt
```

To install them in a virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
python -m pip install -r requirements.txt
```

## Run locally

Run the scraper:

```bash
python script/web-scraping-apec-pdb.py
```

Render the dashboard:

```bash
quarto render dashboard/dashboard-apec-projects.qmd
```

Deploy the dashboard manually to shinyapps.io:

```bash
Rscript -e 'rsconnect::deployApp(
  appDir = "dashboard",
  appPrimaryDoc = "dashboard-apec-projects.qmd",
  appName = "dashboard-apec-projects",
  account = "lfacostaz",
  server = "shinyapps.io",
  forceUpdate = TRUE
)'
```

The updated datasets are written to `data/`, while the dashboard deployment uses the Excel copy stored in `dashboard/`.