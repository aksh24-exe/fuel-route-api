# Fuel Route API

An API that routes between two US locations and returns the cheapest sequence of
truckstops to fuel at along the way, for a vehicle with a 500-mile range at
10 mpg.

```
GET /api/route/?start=Chicago, IL&finish=Dallas, TX
```

```json
{
  "route": { "total_distance_miles": 966.7 },
  "fuel_stops": [
    { "sequence": 1, "truckstop_name": "DEERFIELD TRAVEL CENTER",
      "city": "Steele", "state": "MO", "route_offset_miles": 451.3,
      "price_per_gallon": 2.976, "gallons": 29.04, "cost": 86.42 }
  ],
  "totals": {
    "gallons_purchased": 46.67,
    "total_fuel_cost": 136.07,
    "baseline_cost": 153.17,
    "savings": 17.10,
    "savings_percent": 11.2,
    "fuel_cost_per_mile": 0.141
  },
  "meta": { "routing_api_calls": 1, "geocoding_api_calls": 0, "elapsed_ms": 1161.6 }
}
```

Add `&format=html` to the same URL to get the route drawn on a map.

---

## Running it

With [uv](https://docs.astral.sh/uv/):

```bash
uv sync
uv run manage.py migrate
uv run manage.py load_fuel_prices
uv run manage.py runserver
```

With pip:

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python manage.py migrate
python manage.py load_fuel_prices
python manage.py runserver
```

Then open <http://127.0.0.1:8000/>, pick two cities, and plan the route. The same result is available directly as <http://127.0.0.1:8000/api/route/?start=Chicago,%20IL&finish=Dallas,%20TX&format=html>.

**No API keys are needed.** That was a deliberate constraint: OSRM's public demo
server, the GeoNames gazetteer and OpenStreetMap tiles are all free and
keyless, so this repository runs immediately after cloning.

`load_fuel_prices` takes a few seconds and makes **no network calls** — see below.

---

## Deploying on EC2

The image runs Gunicorn. SQLite is not copied into the image. The container writes `db.sqlite3` to `/data`, and Compose bind-mounts that directory to `./.data` on the instance, so the database stays on the EC2 disk when you rebuild.

On the instance, after Docker is installed and port 8000 is open in the security group:

```bash
git clone <this repository>
cd fuel-route-api
DJANGO_ALLOWED_HOSTS=<public-ip-or-dns> docker compose up -d --build
```

Open `http://<public-ip>:8000/`. The first start migrates and loads the fuel prices into `./.data/db.sqlite3`. Later starts reuse that file.

The instance needs outbound HTTPS so it can reach the OSRM demo server. Set `DJANGO_SECRET_KEY` to a long random value before anyone else can reach the box. `DJANGO_ALLOWED_HOSTS=*` is enough for a first check; replace it with the instance's public IP or DNS name after that.

---

## The constraint that shaped the design

The supplied price file gives a city and a state for each truckstop and **no
coordinates**:

```
7,WOODSHED OF BIG CABIN,"I-44, EXIT 283 & US-69",Big Cabin,OK,307,3.00733333
```

You cannot ask "is this truckstop near my route?" without coordinates, so
6,738 addresses have to become points somewhere. Doing it on demand would mean
thousands of HTTP requests against geocoders that rate-limit to roughly one per
second — hours per request — and would add a second upstream the API depends on.

So the work splits into two phases.

### Build time — once, offline, committed

```
GeoNames US.txt (2,241,404 rows)          data/fuel-prices-...csv (8,151 rows)
        │ keep feature class P                        │
        ▼                                             │
data/us_gazetteer.csv.gz  ──── lookup (city, state) ──┤
190,795 keys, 2.3 MB, in the repo                     ▼
                                          manage.py load_fuel_prices
                                                      ▼
                                          SQLite: 6,607 truckstops with coordinates
```

The gazetteer is generated from the GeoNames US export (public domain) and
**checked into the repository**, so importing the price file resolves every
truckstop with zero API calls. Measured: **99.73%** of in-scope US rows resolve.

Regenerating it is a development step, not something a reviewer needs:

```bash
curl -o /tmp/US.zip https://download.geonames.org/export/dump/US.zip
uv run manage.py build_gazetteer --source /tmp/US.zip --verify
```

### Request time — one external call

```
GET /api/route/
  ├─ geocode both endpoints        dictionary lookup, 0 calls
  ├─ OSRM /route/v1/driving        ← the only external call
  ├─ resample the polyline         cumulative mile markers
  ├─ corridor search               grid index → nearby truckstops
  ├─ optimizer                     which stops, how many gallons
  └─ response                      JSON, or a Leaflet map
```

OSRM returns the geometry **and** the total distance in one response, so there
is no second request to fetch the shape. That response is cached on the rounded
coordinate pair, so a repeated query costs **zero** calls: measured 1,162 ms
cold, **40 ms** warm.

The response says so itself rather than asking anyone to take it on trust:

```json
"meta": { "routing_api_calls": 1, "geocoding_api_calls": 0, "cache_hit": false }
```

---

## How the corridor search stays fast

Chicago to Dallas comes back as a 9,256-vertex polyline. Testing every
truckstop against every vertex is **61,154,392** distance computations per
request, and it grows with route length.

Two changes remove that without approximating the answer:

- the route is resampled to one point every ~2 miles (9,256 → 447 points),
  because a 500-mile range problem does not need metre-level resolution to
  decide what is nearby;
- truckstops live in a latitude/longitude grid built once per process, so a
  request only opens the cells the route passes through.

Measured on that route: **33 ms**, finding 158 truckstops within 5 miles. A
truckstop outside the corridor could never have been a candidate, so nothing is
lost by never looking at it.

---

## The optimizer

Tank capacity follows from the brief: `500 miles ÷ 10 mpg = 50 gallons`.

Candidates are first thinned — bucketed into 25-mile bins along the route,
keeping the cheapest few per bin — which leaves a genuinely distinct set to
choose from and keeps the map legible. Bins are far shorter than the vehicle's
range, so this cannot remove the only reachable truckstop in a stretch.

Then, at every stop, one rule:

- **if a cheaper truckstop is reachable, buy only enough fuel to reach it** —
  every extra gallon bought here is one not bought at the better price;
- **if nothing ahead is cheaper, fill the tank** and drive to the cheapest
  truckstop within range;
- **if the destination is in range, buy exactly enough to arrive.**

This is the classic gas station problem, where greedy is provably optimal.

You can see it working in a real response — at Texarkana it buys just 1.3
gallons, because a cheaper truckstop sits 13 miles further on:

```
1. mile 451.3  $2.976  29.04 gal  $86.42  DEERFIELD TRAVEL CENTER  Steele, MO
2. mile 790.4  $2.857   1.30 gal  $ 3.71  Quiktrip #7900           Texarkana, TX
3. mile 803.4  $2.817  12.19 gal  $34.34  EXTRA MILE TRUCK STOP    Hooks, TX
4. mile 925.3  $2.801   4.14 gal  $11.60  CADOO MILLS              Caddo Mills, TX
```

---

## Assumptions, stated plainly

The brief leaves some things open. Each one is a setting in
`config/settings.py` under `FUEL_ROUTE`, not a number buried in the code.

**Starting fuel — assumed a full tank.** The brief does not say, and it changes
every figure in the response: an empty tank forces a stop at mile 0, while a
full one means any route under 500 miles costs nothing. Override per request
with `?start_gallons=0`.

**Repeated price observations — averaged, not minimised.** The price file holds
8,151 rows against 6,738 distinct OPIS IDs, and **597 of the repeated IDs carry
different prices for the same truckstop in the same city**:

```
id 105  TA SAGINAW I 75 TRAVEL CENTER | Bridgeport MI
        3.269  3.339  3.429  3.289  3.399  3.299
```

It is an OPIS retail feed with several observations per site and no date column
to separate them. Each truckstop's price is therefore the **mean** of its
observations. Taking the minimum would make every route look cheaper and the
savings figure look better, but it would be a promise the data cannot keep —
nothing says the truck arrives on the cheap day. `observation_count` on each
row keeps that collapsing visible.

**Truckstops resolve to city centre, not their interstate exit.** The gazetteer
places a site at the middle of its city, an error of a few miles. Against a
500-mile range that does not change which stops are optimal.

**Savings are measured against the corridor average.** `baseline_cost` is the
same gallons bought at the mean price of the truckstops along this route —
roughly what a driver refuelling without price information would pay. It is
deliberately not the worst price available, which would flatter the result.

**Detour fuel is not charged.** Leaving the highway and returning burns real
miles. Rather than invent a precision the data cannot support, the corridor
stays tight at 5 miles (`?max_detour_miles=`), truckstops rank on price and
tie-break on detour, and the detour is reported per stop so you can see it.

### Deliberately out of scope

- **Tolls.** Real corridor costing needs them; the brief supplies only fuel prices.
- **IFTA fuel tax.** Carriers optimise on state tax differentials, not just pump price.
- **Hours-of-service.** Where a driver *must* stop is a different constraint from where fuel is cheap.
- **Canadian truckstops.** The feed contains them; the brief scopes routes to the USA, so they are skipped and counted, not silently dropped.

---

## API reference

### `GET /api/route/`

| Parameter | Required | Default | Notes |
|---|---|---|---|
| `start` | yes | — | `"Chicago, IL"`, `"Chicago"`, `"Dallas, Texas"`, or `"41.8781,-87.6298"` |
| `finish` | yes | — | same forms |
| `start_gallons` | no | full tank | `0` to depart empty; capped at tank capacity |
| `max_detour_miles` | no | `5` | corridor half-width, max 50 |
| `format` | no | json | `html` renders the map |

### Status codes

| Code | `error.code` | When |
|---|---|---|
| 400 | `invalid_parameters` | a parameter is missing or out of range |
| 400 | `location_not_found` | the place could not be resolved, or is in Canada |
| 422 | `no_route` | no road connects the two points |
| 422 | `route_not_fuelable` | a stretch is longer than the range; the response names the gap |
| 503 | `routing_unavailable` | OSRM could not be reached |

---

## Tests

```bash
uv run manage.py test
```

94 tests, ~0.9 s, and **no network access** — the routing call is stubbed, which
is also the cleanest way to assert the thing the brief asks about: that one
request makes exactly one call, and a repeat makes none.

The optimizer is tested against a worked example whose arithmetic is in the test
docstring, so a greedy rule that looked plausible but bought a few gallons too
many at the wrong stop would fail rather than return a sensible-looking answer.

---

## Layout

```
config/settings.py                  FUEL_ROUTE: every assumption in one place
routing/
  models.py                         FuelStation
  views.py                          the endpoint; orders the pipeline, maps errors
  serializers.py                    query validation
  services/
    geo.py                          haversine, point-to-segment
    geocode.py                      offline gazetteer, name normalisation
    osrm.py                         the only network call; polyline decoding, caching
    corridor.py                     resampling, grid index, thinning
    optimizer.py                    the greedy fuel plan
  management/commands/
    build_gazetteer.py              dev tool: GeoNames → committed gazetteer
    load_fuel_prices.py             CSV → database, geocoded offline
  tests/                            88 tests
templates/index.html                city picker; calls the route API
templates/map.html                  Leaflet map for ?format=html
Dockerfile                          image for EC2
docker-compose.yml                  publishes port 8000 and keeps SQLite on the host
data/
  fuel-prices-for-be-assessment.csv the supplied price file
  us_gazetteer.csv.gz               190,795 keys, committed
```

---

## Attribution

- Routing: [OSRM](https://project-osrm.org/) public demo server
- Geocoding: [GeoNames](https://www.geonames.org/) US export, CC BY 4.0
- Tiles: [OpenStreetMap](https://www.openstreetmap.org/copyright) contributors
- Prices: OPIS retail feed, as supplied with the assessment
