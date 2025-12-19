# Trip Planner Web App

A simple web-based trip planner built with Flask. It focuses on:

- A "My Trips" dashboard.
- Trip creation and editing with destination, dates, and travel style.
- A day-by-day itinerary with morning/afternoon/evening slots (each with notes and a Google Maps link).
- A simple budget tracker per trip.
- Opinionated "AI-style" helpers and foodie-focused ideas you can later wire up to real AI APIs.
 - Per-day checklists for places to visit, recommended restaurants, and hotels/stays, with auto-suggested examples.
 - Optional multi-location stops (cities/regions within a country) with their own date ranges.

## Project Structure

- `app.py` — main Flask application and routes.
- `templates/` — HTML templates (dashboard, trip form, trip detail, simple recipe page).
- `static/styles.css` — basic styling.
- `trip_planner.sqlite` — SQLite database file (auto-created in the project folder).

## Prerequisites

- Python 3.10+ (you appear to have Python 3.13 already).
- A virtual environment is recommended.

## Setup and Run

From the project root (`/Users/chunghsu/projects/trip-planner`):

```bash
python -m venv .venv
source .venv/bin/activate  # On Windows: .venv\Scripts\activate
pip install -r requirements.txt
python app.py
```

Then open `http://127.0.0.1:5000/` in your browser.

## Core Features Implemented (Phase 1)

- **User Dashboard**
  - `GET /trips` (also `/`) lists upcoming and past trips.

- **Trip Creation Form**
  - `GET /trip/new` shows the form.
  - `POST /trip/new` creates a trip and auto-generates empty itinerary days between the start and end dates.

- **Day-by-Day Timeline**
  - `GET /trip/<id>` shows all days for a trip, each with:
    - Morning, afternoon, evening slots.
    - Title, description, and an optional Google Maps URL per slot.
  - `POST /trip/<id>/itinerary` saves all itinerary updates.
  - For each day, there are checklists for:
    - Places to visit.
    - Recommended restaurants.
    - Hotels / stays.
  - You can tick items you’ve decided on and add your own custom entries inline.

- **Simple Budget Tracker**
  - `POST /trip/<id>/budget` adds budget items with estimated vs. actual cost.
  - The trip page shows a table plus totals.

- **Edit Existing Trips**
  - `GET /trip/<id>/edit` shows an edit form for destination, dates, and travel style.
  - `POST /trip/<id>/edit` updates the trip and adjusts the day-by-day timeline when dates change (keeping any notes for dates that still exist).

- **Multi-Location Stops**
  - On the trip page you can add “Trip Stops” (cities/regions within the same country) with their own start/end dates.
  - Stops must fall within the overall trip dates.
  - The trip sidebar lists all stops, and each day header shows which stop/location it belongs to if there is one.

## "Secret Sauce" and Foodie Features (Stubs)

These are implemented in a simple, offline-friendly way so you can later replace them with real AI calls and APIs:

- **One-Click Itinerary Generator**
  - Button on the trip page posts to `POST /trip/<id>/generate-itinerary`.
  - Uses `build_ai_style_itinerary` in `app.py` to auto-fill each day with suggestions based on travel style (Budget, Luxury, Family, Foodie, Flexible).

- **Smart Weather Integration**
  - `build_weather_stub` now supports live 7-day forecasts via the OpenWeather API when configured.
  - If no key is set or the API fails, it falls back to a simple offline placeholder forecast.

- **"Terrible Cook" Local Recipe Finder**
  - Link on the trip page goes to `GET /trip/<id>/recipe`.
  - Uses `build_simple_local_recipe` (you can customize/extend) to generate a one-pan style recipe with a handful of ingredients and ultra-simple steps.

- **Dynamic Packing List**
  - `build_packing_list` looks at destination keywords and trip dates to suggest categories like Clothing, Weather, Activities, and Foodie Tools.
  - Displayed on the trip side panel.

- **Foodie Twist**
  - `build_foodie_highlights` suggests:
    - Dishes to try.
    - Hidden gem ideas.
    - A small grocery list for cooking in an Airbnb.
  - There are special cases for Charleston and Tokyo and a generic fallback.

- **Daily Places/Restaurants/Hotels (Checklists)**
  - Button on the trip page posts to `POST /trip/<id>/suggest-places`.
  - Uses `build_day_suggestions` to add per-day suggestions for:
    - Places to visit.
    - Restaurants to consider.
    - Hotels or stays.
  - If you set a Google Places API key (see below), suggestions come from the live Google Places Text Search API.
  - If there is no API key or the API fails, it falls back to simple offline, destination-based suggestions.
  - All suggestions appear as unticked checkboxes so you can select what you actually want to do.
  - You can also type in your own entries for each day and category; they are saved when you click “Save Itinerary”.

## Future "Garnish" Ideas

These are not fully implemented yet, but you can add them as you go:

- **PDF Export**
  - Add a route (e.g. `/trip/<id>/export/pdf`) that renders the itinerary into a print-styled HTML page.
  - Later, hook this up to a library like WeasyPrint or wkhtmltopdf to generate PDFs.

- **Photo Gallery**
  - Add a photo URL field to the `trips` table or a new `photos` table.
  - Render one "hero photo" per trip on the dashboard and detail page.

- **Currency Converter**
  - Add a lightweight widget in the side panel for converting from USD to the local currency.
  - Initially use a static rate table; later call a real FX API.

- **Travel Document Vault**
  - Add an `uploads` folder and a small table to track file metadata.
  - Let users upload PDFs or images of passports and tickets.
  - Keep in mind security and privacy if you ever deploy this publicly.

## Where to Add Real AI

When you’re ready to use a provider like OpenAI or another model:

- Replace `build_ai_style_itinerary` with a call that sends:
  - Destination, dates, travel style, and any existing notes.
  - Ask the model to respond in a structured JSON format you can map into morning/afternoon/evening slots.
- Replace `build_simple_local_recipe` and `build_foodie_highlights` similarly, using destination, travel style, and available kitchen gear as inputs.

For now, everything else works locally with no external network calls so you can experiment safely.

## Live Places/Restaurants/Hotels via Google Places

The “Suggest Places/Food/Hotels” button is wired to use the Google Places Text Search API when a key is available, and otherwise falls back to offline suggestions.

To use live data:

1. Create a Google Cloud project and enable the Places API.
2. Create an API key and restrict it to Places / Maps usage as appropriate.
3. Set it in your shell before running the app, for example:

   ```bash
   export GOOGLE_PLACES_API_KEY="your-key-here"
   ```

4. Start the app (`python app.py`) and open a trip page.
5. Click **Suggest Places/Food/Hotels**; the app will:
   - Call Google Places Text Search for tourist attractions, restaurants, and hotels in your destination.
   - Use your travel style (Budget, Luxury, Family, Foodie, etc.) to bias restaurant/hotel queries.
   - Fill in per-day checklists while still falling back to offline suggestions if any category comes back empty or the API errors.

## Live Weather via OpenWeather

The “Weather During Trip” card can use live daily forecasts from OpenWeather.

To enable it:

1. Create an account at OpenWeather and get an API key.
2. Enable the One Call API / daily forecasts for your account.
3. In your terminal, set:

   ```bash
   export OPENWEATHER_API_KEY="your-openweather-key-here"
   ```

4. Start the app (`python app.py`) and open a trip page.
5. The weather section will:
   - Geocode your destination to latitude/longitude using OpenWeather’s Geo API.
   - Fetch a 7-day daily forecast using the One Call API (metric units).
   - Show a short description plus high/low temperatures per day.
   - Fall back to an offline placeholder forecast if anything fails.
