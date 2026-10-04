# Weyland Energy – OpenCloud demo data

Realistic demo data for an OpenCloud dev or demo instance: a fictional renewable energy
company with **1,000 users** (with profile pictures), **25 project spaces**, **~5,400 files**
with real content (ODF, Office, PDF, images), version history, tags, shares between users,
folder shares for external partners and public links.

## Quick start

```bash
cd weyland-energy-demo-data
python3 populate.py
```

The script asks for three things – press **Enter** to accept the default:

```
OpenCloud URL   [https://host.docker.internal:9200]
Admin user      [admin]
Admin password  [*****]        (default: admin)
```

That's it. A full run takes about **15–25 minutes** on a local instance. You can run it again at any
time – it only adds what is missing and updates what changed.

macOS/Linux: `./populate.sh` (both just call `python3 populate.py`).

## What gets created

| | |
|---|---|
| Users | 1,000: 450 employees (Hamburg, Edinburgh, Copenhagen), 500 external partners from 19 companies, 5 supervisory board members, 45 former employees (disabled) |
| Groups | departments, offices, project teams, partner companies (`ext-…`), `all-staff`, … |
| Spaces | 25 – projects (e.g. Harrow Bay Offshore Wind), departments (Finance, HR, Legal, …), company-wide (Handbook, Brand Assets, …) with description, image and readme |
| Access | realistic: departments edit their space, project teams their project, everyone reads the handbook; confidential spaces restricted; externals only get the folders they work on |
| Files | ~1,100 company documents with real, consistent content + ~4,300 personal files; original modification dates and older versions |
| Sharing | ~4,600 shares between users (≈ 5 per user), 50+ public links, tags on ~600 files |
| Search | try `HB-RISK-017`, `harbour porpoise`, `WE-PPA-2026-004`, `skylark`, `Arcturus` |

`data/metadata.json` describes everything: company, users, groups, spaces, members, every file
(title, author, dates, tags, shares, versions) and suggested search terms.

### No questions asked

```bash
python3 populate.py --yes                                          # use the defaults
python3 populate.py --url https://localhost:9200 --admin admin:secret
OC_URL=https://cloud.example OC_ADMIN_USER=admin OC_ADMIN_PASSWORD=secret python3 populate.py --yes
```

Or edit the `SETTINGS` block at the top of `populate.py` once.

## Requirements

- **Python 3.9+** – nothing to install, the script uses only the standard library.
- **An OpenCloud admin account.** An app token works instead of the password.
- **Basic auth enabled on the server** – the script logs in as the demo users to upload their
  personal files and profile pictures. In OpenCloud: `PROXY_ENABLE_BASIC_AUTH=true`.
- Optional: favourites are only set if they are enabled on the server.

## Log in as a demo user

All demo users have the password **`demo`**. Good accounts for demos:

| User | Who | Sees |
|---|---|---|
| `elena.marquez` | Project Director, Harrow Bay Offshore Wind | the flagship project, lots of shares |
| `liam.mcallister` | Project Manager, Millbrook Solar & Storage | planning, community engagement |
| `amara.okafor` | CEO | almost everything, board space |
| `david.chen` | Asset Operations Manager | Kestrel Ridge wind farm, operations |
| `julia.hoffmann` | HR Business Partner | HR (confidential), handbook, onboarding |
| `lars.henriksen` | External – Halden Turbine Systems | only folders shared with his company |
| `rachel.moss` | External – Marlow Ecology | only environmental folders |

Public links are protected with the password **`Weyland-2026!`**.



## Remove the demo data

```bash
python3 populate.py --reset
```

Deletes the 25 spaces, the 1,000 users (incl. their personal files) and the groups of this dataset.

## Troubleshooting

| Problem | Fix |
|---|---|
| `Cannot reach https://host.docker.internal:9200` | Use the URL you open in your browser, e.g. `https://localhost:9200`. |
| `Login failed (401)` | Wrong password, or basic auth is disabled (`PROXY_ENABLE_BASIC_AUTH=true`). |
| Personal files / profile pictures skipped | The demo users already existed with another password – run with `--user-password <their password>` or reset first. |
| Run was interrupted | Just start it again. |

All companies, people and numbers are fictional. All Content, faces and photos are generated.
