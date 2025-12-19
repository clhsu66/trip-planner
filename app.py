import csv
import io
import os
import sqlite3
import urllib.parse
from datetime import datetime, timedelta

import requests
from flask import Flask, redirect, render_template, request, url_for, flash, request

try:  # Optional local configuration for API keys, kept out of git.
    import config_local  # type: ignore[import]
except ImportError:
    config_local = None  # type: ignore[assignment]


def _apply_local_config_env():
    """
    If a config_local.py file exists, copy any API keys from it into
    environment variables so the rest of the app can keep using
    os.environ[...] without changes.
    """
    global config_local  # type: ignore[global-var-not-assigned]
    if config_local is None:
        return

    google_key = getattr(config_local, "GOOGLE_PLACES_API_KEY", None)
    if google_key and not os.environ.get("GOOGLE_PLACES_API_KEY"):
        os.environ["GOOGLE_PLACES_API_KEY"] = google_key

    weather_key = getattr(config_local, "OPENWEATHER_API_KEY", None)
    if weather_key and not os.environ.get("OPENWEATHER_API_KEY"):
        os.environ["OPENWEATHER_API_KEY"] = weather_key


_apply_local_config_env()


def google_maps_url(name: str | None) -> str:
    if not name:
        return ""
    return "https://www.google.com/maps/search/?api=1&query=" + urllib.parse.quote_plus(str(name))


def build_day_directions_url(trip, day, day_items_by_category, city: str | None) -> str:
    """
    Build a Google Maps directions URL for a single day:
    start at hotel -> breakfast spots -> places -> back to hotel.
    """
    if day_items_by_category is None:
        return ""

    city_text = city or trip["destination"]

    hotels = [
        item
        for item in day_items_by_category.get("hotel", [])
        if item["checked"]
    ]
    if not hotels:
        # No starting/ending hotel for this day.
        return ""

    hotel = hotels[0]
    hotel_location = f"{hotel['name']}, {city_text}"

    breakfast_spots = [
        item
        for item in day_items_by_category.get("restaurant", [])
        if item["checked"] and (item["slot"] or "").lower() == "breakfast"
    ]
    activities = [
        item
        for item in day_items_by_category.get("place", [])
        if item["checked"]
    ]

    waypoints = []
    for item in breakfast_spots:
        waypoints.append(f"{item['name']}, {city_text}")
    for item in activities:
        waypoints.append(f"{item['name']}, {city_text}")

    if not waypoints:
        # Nothing to route through.
        return ""

    origin = hotel_location
    destination = hotel_location

    base = "https://www.google.com/maps/dir/?api=1"
    parts = [
        base,
        "origin=" + urllib.parse.quote_plus(origin),
        "destination=" + urllib.parse.quote_plus(destination),
    ]

    if waypoints:
        # Google Maps expects waypoints joined by pipe characters.
        joined = "|".join(waypoints)
        parts.append("waypoints=" + urllib.parse.quote_plus(joined))

    parts.append("travelmode=driving")
    return "&".join(parts)


def create_app():
    app = Flask(__name__)
    app.config["SECRET_KEY"] = "dev-change-me"
    app.config["DATABASE"] = os.path.join(app.root_path, "trip_planner.sqlite")

    ensure_database(app)

    # Template filter to build a Google Maps search URL for a place name.
    app.jinja_env.filters["google_maps_url"] = google_maps_url

    @app.route("/")
    def index():
        return redirect(url_for("list_trips"))

    @app.route("/trips")
    def list_trips():
        db = get_db(app)
        rows = db.execute(
            "SELECT id, destination, start_date, end_date, travel_style "
            "FROM trips ORDER BY start_date"
        ).fetchall()

        # Compute a simple planning status for each trip.
        status_by_trip: dict[int, str] = {}
        for row in rows:
            trip_id = row["id"]
            # Load itinerary days.
            days = db.execute(
                """
                SELECT id, morning_description, afternoon_description, evening_description
                FROM itinerary
                WHERE trip_id = ?
                """,
                (trip_id,),
            ).fetchall()
            if not days:
                status_by_trip[trip_id] = "Planning"
                continue

            day_ids = [day["id"] for day in days]
            placeholders = ",".join("?" for _ in day_ids)
            items = db.execute(
                f"""
                SELECT itinerary_id, category, checked
                FROM day_items
                WHERE itinerary_id IN ({placeholders})
                  AND (hidden IS NULL OR hidden = 0)
                """,
                day_ids,
            ).fetchall()

            has_activity_by_day: dict[int, bool] = {day_id: False for day_id in day_ids}
            for day in days:
                if (
                    (day["morning_description"] or "").strip()
                    or (day["afternoon_description"] or "").strip()
                    or (day["evening_description"] or "").strip()
                ):
                    has_activity_by_day[day["id"]] = True
            for item in items:
                if item["checked"]:
                    has_activity_by_day[item["itinerary_id"]] = True

            days_with_activity = sum(1 for v in has_activity_by_day.values() if v)
            total_days = len(day_ids)
            if days_with_activity == 0:
                status = "Planning"
            else:
                ratio = days_with_activity / float(total_days)
                if ratio < 0.5:
                    status = "Planning"
                elif ratio < 0.9:
                    status = "Mostly planned"
                else:
                    status = "Ready"
            status_by_trip[trip_id] = status

        db.close()

        today = datetime.today().date()
        upcoming = []
        past = []
        for row in rows:
            end_date = parse_date(row["end_date"])
            (upcoming if end_date >= today else past).append(row)

        return render_template(
            "dashboard.html",
            upcoming_trips=upcoming,
            past_trips=past,
             status_by_trip=status_by_trip,
        )

    @app.route("/trips/import-csv", methods=["GET", "POST"])
    def import_trip_csv():
        if request.method == "POST":
            file = request.files.get("file")
            destination = (request.form.get("destination") or "").strip()
            travel_style = request.form.get("travel_style") or "Flexible"

            if not destination or not file or not file.filename:
                flash("Please provide a destination and choose a CSV file.")
                return render_template("import_trip.html")

            try:
                raw = file.read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin1")
            except Exception:
                flash("Could not read the uploaded file.")
                return render_template("import_trip.html")

            reader = csv.DictReader(io.StringIO(text))
            parsed_rows = []
            for row in reader:
                date_str = (row.get("date") or "").strip()
                name = (row.get("name") or "").strip()
                if not date_str or not name:
                    continue
                try:
                    date = parse_date(date_str)
                except ValueError:
                    continue

                time_of_day = (row.get("time_of_day") or "").strip().lower()
                category = (row.get("category") or "").strip().lower()
                city = (row.get("city") or "").strip()
                meal = (row.get("meal") or "").strip().lower()

                selected_raw = (row.get("selected") or "").strip().lower()
                if selected_raw in {"0", "false", "no", "n"}:
                    selected = 0
                else:
                    selected = 1

                if category not in {"place", "restaurant", "hotel"}:
                    category = "place"

                parsed_rows.append(
                    {
                        "date": date,
                        "name": name,
                        "time_of_day": time_of_day,
                        "category": category,
                        "city": city,
                        "meal": meal,
                        "selected": selected,
                    }
                )

            if not parsed_rows:
                flash("No valid rows found in CSV. Expected headers: date,time_of_day,category,name,city,meal.")
                return render_template("import_trip.html")

            dates = [r["date"] for r in parsed_rows]
            start_date = min(dates)
            end_date = max(dates)

            db = get_db(app)
            cursor = db.execute(
                """
                INSERT INTO trips (destination, start_date, end_date, travel_style)
                VALUES (?, ?, ?, ?)
                """,
                (destination, start_date.isoformat(), end_date.isoformat(), travel_style),
            )
            trip_id = cursor.lastrowid

            # Build itinerary days for the range.
            day_number = 1
            current = start_date
            while current <= end_date:
                db.execute(
                    """
                    INSERT INTO itinerary (trip_id, day_number, date)
                    VALUES (?, ?, ?)
                    """,
                    (trip_id, day_number, current.isoformat()),
                )
                day_number += 1
                current += timedelta(days=1)

            # Reload itinerary rows keyed by date.
            days = db.execute(
                """
                SELECT id, date
                FROM itinerary
                WHERE trip_id = ?
                """,
                (trip_id,),
            ).fetchall()
            day_by_date = {row["date"]: row for row in days}

            # Build stops per city.
            city_dates: dict[str, list[datetime.date]] = {}
            for row in parsed_rows:
                if row["city"]:
                    city_dates.setdefault(row["city"], []).append(row["date"])

            for city_name, cds in city_dates.items():
                stop_start = min(cds)
                stop_end = max(cds)
                db.execute(
                    """
                    INSERT INTO trip_stops (trip_id, name, start_date, end_date)
                    VALUES (?, ?, ?, ?)
                    """,
                    (trip_id, city_name, stop_start.isoformat(), stop_end.isoformat()),
                )

            # Insert day items from CSV rows.
            for row in parsed_rows:
                date_key = row["date"].isoformat()
                itinerary_row = day_by_date.get(date_key)
                if not itinerary_row:
                    continue

                slot = None
                if row["category"] == "place":
                    if row["time_of_day"] in {"morning", "afternoon", "evening"}:
                        slot = row["time_of_day"]
                elif row["category"] == "restaurant":
                    if row["meal"] in {"breakfast", "lunch", "dinner", "snack"}:
                        slot = row["meal"]

                db.execute(
                    """
                    INSERT INTO day_items (itinerary_id, category, name, checked, slot)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (itinerary_row["id"], row["category"], row["name"], row["selected"], slot),
                )

            db.commit()
            db.close()
            flash("Trip imported from CSV.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        return render_template("import_trip.html")

    @app.route("/trip/<int:trip_id>/import-csv", methods=["GET", "POST"])
    def import_trip_csv_into_trip(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()

        if trip is None:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        if request.method == "POST":
            file = request.files.get("file")
            if not file or not file.filename:
                db.close()
                flash("Please choose a CSV file.")
                return render_template("import_trip_into_existing.html", trip=trip)

            try:
                raw = file.read()
                try:
                    text = raw.decode("utf-8")
                except UnicodeDecodeError:
                    text = raw.decode("latin1")
            except Exception:
                db.close()
                flash("Could not read the uploaded file.")
                return render_template("import_trip_into_existing.html", trip=trip)

            reader = csv.DictReader(io.StringIO(text))
            parsed_rows = []
            for row in reader:
                date_str = (row.get("date") or "").strip()
                name = (row.get("name") or "").strip()
                if not date_str or not name:
                    continue
                try:
                    date = parse_date(date_str)
                except ValueError:
                    continue

                time_of_day = (row.get("time_of_day") or "").strip().lower()
                category = (row.get("category") or "").strip().lower()
                city = (row.get("city") or "").strip()
                meal = (row.get("meal") or "").strip().lower()
                selected_raw = (row.get("selected") or "").strip().lower()
                selected = 0 if selected_raw in {"0", "false", "no", "n"} else 1

                if category not in {"place", "restaurant", "hotel"}:
                    category = "place"

                parsed_rows.append(
                    {
                        "date": date,
                        "name": name,
                        "time_of_day": time_of_day,
                        "category": category,
                        "city": city,
                        "meal": meal,
                        "selected": selected,
                    }
                )

            if not parsed_rows:
                db.close()
                flash("No valid rows found in CSV. Expected headers: date,time_of_day,category,name,city,meal,selected.")
                return render_template("import_trip_into_existing.html", trip=trip)

            trip_start = parse_date(trip["start_date"])
            trip_end = parse_date(trip["end_date"])

            # Only keep rows within the current trip date range.
            in_range_rows = [
                r for r in parsed_rows if trip_start <= r["date"] <= trip_end
            ]
            if not in_range_rows:
                db.close()
                flash("CSV rows are all outside the current trip dates.")
                return render_template("import_trip_into_existing.html", trip=trip)

            itinerary_rows = db.execute(
                "SELECT id, date FROM itinerary WHERE trip_id = ?",
                (trip_id,),
            ).fetchall()
            day_by_date = {row["date"]: row for row in itinerary_rows}

            # Add stops from CSV city values (appended to existing stops).
            city_dates: dict[str, list[datetime.date]] = {}
            for row in in_range_rows:
                if row["city"]:
                    city_dates.setdefault(row["city"], []).append(row["date"])

            for city_name, cds in city_dates.items():
                stop_start = min(cds)
                stop_end = max(cds)
                db.execute(
                    """
                    INSERT INTO trip_stops (trip_id, name, start_date, end_date)
                    VALUES (?, ?, ?, ?)
                    """,
                    (trip_id, city_name, stop_start.isoformat(), stop_end.isoformat()),
                )

            # Insert day items (appended to existing items).
            for row in in_range_rows:
                date_key = row["date"].isoformat()
                itinerary_row = day_by_date.get(date_key)
                if not itinerary_row:
                    continue

                slot = None
                if row["category"] == "place":
                    if row["time_of_day"] in {"morning", "afternoon", "evening"}:
                        slot = row["time_of_day"]
                elif row["category"] == "restaurant":
                    if row["meal"] in {"breakfast", "lunch", "dinner", "snack"}:
                        slot = row["meal"]

                db.execute(
                    """
                    INSERT INTO day_items (itinerary_id, category, name, checked, slot)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (itinerary_row["id"], row["category"], row["name"], row["selected"], slot),
                )

            db.commit()
            db.close()
            flash("Trip updated from CSV.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        db.close()
        return render_template("import_trip_into_existing.html", trip=trip)

    @app.route("/trip/<int:trip_id>/export-csv")
    def export_trip_csv(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()

        if trip is None:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        days = db.execute(
            """
            SELECT id, date
            FROM itinerary
            WHERE trip_id = ?
            ORDER BY day_number
            """,
            (trip_id,),
        ).fetchall()

        stops = db.execute(
            """
            SELECT name, start_date, end_date
            FROM trip_stops
            WHERE trip_id = ?
            ORDER BY start_date
            """,
            (trip_id,),
        ).fetchall()

        location_by_date: dict[str, str] = {}
        for stop in stops:
            stop_start = parse_date(stop["start_date"])
            stop_end = parse_date(stop["end_date"])
            current = stop_start
            while current <= stop_end:
                location_by_date[current.isoformat()] = stop["name"]
                current += timedelta(days=1)

        day_ids = [day["id"] for day in days]
        items_by_day: dict[int, list[sqlite3.Row]] = {}
        if day_ids:
            placeholders = ",".join("?" for _ in day_ids)
            rows = db.execute(
                f"""
                SELECT id, itinerary_id, category, name, checked, slot
                FROM day_items
                WHERE itinerary_id IN ({placeholders})
                  AND (hidden IS NULL OR hidden = 0)
                ORDER BY id
                """,
                day_ids,
            ).fetchall()
            for row in rows:
                items_by_day.setdefault(row["itinerary_id"], []).append(row)

        db.close()

        output = io.StringIO()
        writer = csv.writer(output)
        writer.writerow(["date", "time_of_day", "category", "name", "city", "meal", "selected"])

        destination = trip["destination"]
        for day in days:
            date_str = day["date"]
            city = location_by_date.get(date_str) or destination
            day_items = items_by_day.get(day["id"], [])
            for item in day_items:
                category = item["category"]
                name = item["name"]
                slot = item["slot"] or ""
                time_of_day = ""
                meal = ""
                if category == "place":
                    if slot in {"morning", "afternoon", "evening"}:
                        time_of_day = slot
                elif category == "restaurant":
                    if slot in {"breakfast", "lunch", "dinner", "snack"}:
                        meal = slot
                selected = "1" if item["checked"] else "0"
                writer.writerow(
                    [
                        date_str,
                        time_of_day,
                        category,
                        name,
                        city,
                        meal,
                        selected,
                    ]
                )

        csv_data = output.getvalue()
        output.close()

        from flask import Response

        filename = f"trip_{trip_id}_itinerary.csv"
        return Response(
            csv_data,
            mimetype="text/csv",
            headers={"Content-Disposition": f"attachment; filename={filename}"},
        )

    @app.route("/trip/new", methods=["GET", "POST"])
    def new_trip():
        if request.method == "POST":
            destination = (request.form.get("destination") or "").strip()
            start_str = request.form.get("start_date")
            end_str = request.form.get("end_date")
            travel_style = request.form.get("travel_style") or "Flexible"

            if not destination or not start_str or not end_str:
                flash("Please provide destination, start date, and end date.")
                return render_template("trip_form.html")

            try:
                start_date = parse_date(start_str)
                end_date = parse_date(end_str)
            except ValueError:
                flash("Dates must be in YYYY-MM-DD format.")
                return render_template("trip_form.html")

            if end_date < start_date:
                flash("End date cannot be before start date.")
                return render_template("trip_form.html")

            db = get_db(app)
            cursor = db.execute(
                """
                INSERT INTO trips (destination, start_date, end_date, travel_style)
                VALUES (?, ?, ?, ?)
                """,
                (destination, start_date.isoformat(), end_date.isoformat(), travel_style),
            )
            trip_id = cursor.lastrowid

            day_number = 1
            current = start_date
            while current <= end_date:
                db.execute(
                    """
                    INSERT INTO itinerary (trip_id, day_number, date)
                    VALUES (?, ?, ?)
                    """,
                    (trip_id, day_number, current.isoformat()),
                )
                day_number += 1
                current += timedelta(days=1)

            db.commit()
            db.close()
            flash("Trip created.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        return render_template("trip_form.html")

    @app.route("/trip/<int:trip_id>")
    def trip_detail(trip_id):
        db = get_db(app)
        trip = db.execute(
            """
            SELECT id, destination, start_date, end_date, travel_style
            FROM trips WHERE id = ?
            """,
            (trip_id,),
        ).fetchone()

        if trip is None:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        days = db.execute(
            """
            SELECT *
            FROM itinerary
            WHERE trip_id = ?
            ORDER BY day_number
            """,
            (trip_id,),
        ).fetchall()

        # Day-of-week helper for templates.
        weekday_by_date = {}
        for day in days:
            try:
                date_obj = parse_date(day["date"])
                weekday_by_date[day["date"]] = date_obj.strftime("%A")
            except Exception:
                weekday_by_date[day["date"]] = ""
        # Multi-location stops within this trip.
        stops = db.execute(
            """
            SELECT id, name, start_date, end_date
            FROM trip_stops
            WHERE trip_id = ?
            ORDER BY start_date
            """,
            (trip_id,),
        ).fetchall()

        # Map dates to stop name so each day can show its location.
        location_by_date = {}
        for stop in stops:
            stop_start = parse_date(stop["start_date"])
            stop_end = parse_date(stop["end_date"])
            current = stop_start
            while current <= stop_end:
                location_by_date[current.isoformat()] = stop["name"]
                current += timedelta(days=1)

        # Load checklist items (places, restaurants, hotels) per day.
        day_ids = [day["id"] for day in days]
        items_by_day: dict[int, dict[str, list[sqlite3.Row]]] = {}
        if day_ids:
            placeholders = ",".join("?" for _ in day_ids)
            rows = db.execute(
                f"""
                SELECT id, itinerary_id, category, name, checked, slot
                FROM day_items
                WHERE itinerary_id IN ({placeholders})
                  AND (hidden IS NULL OR hidden = 0)
                ORDER BY id
                """,
                day_ids,
            ).fetchall()
            for row in rows:
                items_by_day.setdefault(
                    row["itinerary_id"],
                    {"place": [], "restaurant": [], "hotel": []},
                )
                items_by_day[row["itinerary_id"]][row["category"]].append(row)

        # Hotels filtered by location for each day (so UI can separate picks vs suggestions cleanly).
        hotels_filtered_by_day: dict[int, list[sqlite3.Row]] = {}

        # Build a Google Maps directions URL and completion stats for each day.
        directions_by_day: dict[int, str] = {}
        completion_by_day: dict[int, dict[str, int]] = {}
        for day in days:
            per_day_items = items_by_day.get(
                day["id"], {"place": [], "restaurant": [], "hotel": []}
            )
            city = location_by_date.get(day["date"]) or trip["destination"]
            url = build_day_directions_url(trip, day, per_day_items, city)
            if url:
                directions_by_day[day["id"]] = url

            # Completion stats: time-of-day slots filled and meals picked.
            slots_filled = 0
            if (day["morning_description"] or "").strip():
                slots_filled += 1
            if (day["afternoon_description"] or "").strip():
                slots_filled += 1
            if (day["evening_description"] or "").strip():
                slots_filled += 1
            total_slots = 3

            restaurants = per_day_items.get("restaurant", [])
            meal_flags = {"breakfast": False, "lunch": False, "dinner": False}
            for item in restaurants:
                slot = (item["slot"] or "").lower()
                if slot in meal_flags and item["checked"]:
                    meal_flags[slot] = True
            meals_picked = sum(1 for v in meal_flags.values() if v)
            total_meals = 3

            denom = total_slots + total_meals
            percent = 0
            if denom:
                percent = int(round((slots_filled + meals_picked) / float(denom) * 100))
            completion_by_day[day["id"]] = {
                "slots_filled": slots_filled,
                "total_slots": total_slots,
                "meals_picked": meals_picked,
                "total_meals": total_meals,
                "percent": percent,
            }

            # Filter hotels by location for this day.
            location_name = location_by_date.get(day["date"])
            hotels = list(per_day_items.get("hotel", []))
            if location_name:
                loc_lower = location_name.lower()
                hotels = [
                    h for h in hotels
                    if loc_lower in (h["name"] or "").lower()
                ]
            hotels_filtered_by_day[day["id"]] = hotels

        budget_items = db.execute(
            """
            SELECT id, label, estimated_cost, actual_cost
            FROM budget_items
            WHERE trip_id = ?
            ORDER BY id
            """,
            (trip_id,),
        ).fetchall()
        total_estimated = sum((item["estimated_cost"] or 0) for item in budget_items)
        total_actual = sum((item["actual_cost"] or 0) for item in budget_items)
        budget_progress_percent = 0
        if total_estimated > 0:
            budget_progress_percent = int(
                round(min(total_actual / float(total_estimated), 1.0) * 100)
            )

        packing_items_by_category = get_packing_items_for_trip(db, trip, days)
        weather_forecast = build_weather_stub(trip, days)

        # Build Foodie Twist highlights per stop/city so multi-city trips
        # can surface suggestions for each location.
        foodie_highlights_by_city: dict[str, dict[str, list[str]]] = {}
        if stops:
            seen_names: set[str] = set()
            for stop in stops:
                city = (stop["name"] or "").strip()
                if not city or city in seen_names:
                    continue
                seen_names.add(city)
                pseudo_trip = dict(trip)
                pseudo_trip["destination"] = city
                foodie_highlights_by_city[city] = build_foodie_highlights(pseudo_trip)
        else:
            foodie_highlights_by_city[trip["destination"]] = build_foodie_highlights(trip)

        db.close()

        using_live_places = bool(os.environ.get("GOOGLE_PLACES_API_KEY"))
        using_live_weather = bool(os.environ.get("OPENWEATHER_API_KEY"))

        return render_template(
            "trip_detail.html",
            trip=trip,
            days=days,
            stops=stops,
            location_by_date=location_by_date,
            items_by_day=items_by_day,
            directions_by_day=directions_by_day,
            completion_by_day=completion_by_day,
            hotels_filtered_by_day=hotels_filtered_by_day,
            budget_items=budget_items,
            total_estimated=total_estimated,
            total_actual=total_actual,
            budget_progress_percent=budget_progress_percent,
            packing_items_by_category=packing_items_by_category,
            weather_forecast=weather_forecast,
            foodie_highlights_by_city=foodie_highlights_by_city,
            weekday_by_date=weekday_by_date,
            using_live_places=using_live_places,
            using_live_weather=using_live_weather,
        )

    @app.route("/trip/<int:trip_id>/itinerary", methods=["POST"])
    def update_itinerary(trip_id):
        db = get_db(app)
        days = db.execute(
            "SELECT id FROM itinerary WHERE trip_id = ? ORDER BY day_number",
            (trip_id,),
        ).fetchall()

        for day in days:
            day_id = day["id"]
            values = []
            fields = [
                "morning_title",
                "morning_description",
                "morning_map_link",
                "afternoon_title",
                "afternoon_description",
                "afternoon_map_link",
                "evening_title",
                "evening_description",
                "evening_map_link",
            ]
            for field in fields:
                form_key = f"{field}_{day_id}"
                values.append((request.form.get(form_key) or "").strip() or None)

            db.execute(
                """
                UPDATE itinerary
                SET
                    morning_title = ?,
                    morning_description = ?,
                    morning_map_link = ?,
                    afternoon_title = ?,
                    afternoon_description = ?,
                    afternoon_map_link = ?,
                    evening_title = ?,
                    evening_description = ?,
                    evening_map_link = ?
                WHERE id = ?
                """,
                (*values, day_id),
            )

            # Update checklist items for this day (places, restaurants, hotels).
            existing_items = db.execute(
                """
                SELECT id, category
                FROM day_items
                WHERE itinerary_id = ?
                """,
                (day_id,),
            ).fetchall()
            for item in existing_items:
                item_id = item["id"]
                category = item["category"]
                checked = 1 if request.form.get(f"item_checked_{item_id}") == "on" else 0

                slot = None
                if category == "place":
                    raw_slot = (request.form.get(f"place_slot_{item_id}") or "").strip().lower()
                    if raw_slot in {"morning", "afternoon", "evening"}:
                        slot = raw_slot
                elif category == "restaurant":
                    raw_slot = (request.form.get(f"restaurant_slot_{item_id}") or "").strip().lower()
                    if raw_slot in {"breakfast", "lunch", "dinner", "snack"}:
                        slot = raw_slot

                db.execute(
                    """
                    UPDATE day_items
                    SET checked = ?, slot = ?
                    WHERE id = ?
                    """,
                    (checked, slot, item_id),
                )

            # Handle any new items typed in by the user.
            new_place = (request.form.get(f"new_place_{day_id}") or "").strip()
            if new_place:
                db.execute(
                    """
                    INSERT INTO day_items (itinerary_id, category, name, checked)
                    VALUES (?, 'place', ?, 1)
                    """,
                    (day_id, new_place),
                )

            new_restaurant = (request.form.get(f"new_restaurant_{day_id}") or "").strip()
            if new_restaurant:
                db.execute(
                    """
                    INSERT INTO day_items (itinerary_id, category, name, checked)
                    VALUES (?, 'restaurant', ?, 1)
                    """,
                    (day_id, new_restaurant),
                )

            new_hotel = (request.form.get(f"new_hotel_{day_id}") or "").strip()
            if new_hotel:
                db.execute(
                    """
                    INSERT INTO day_items (itinerary_id, category, name, checked)
                    VALUES (?, 'hotel', ?, 1)
                    """,
                    (day_id, new_hotel),
                )

        db.commit()
        db.close()
        flash("Itinerary updated.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/day-item/<int:item_id>/hide", methods=["POST"])
    def hide_day_item(trip_id, item_id):
        db = get_db(app)
        # Soft-hide this suggestion while keeping it in the database.
        db.execute(
            """
            UPDATE day_items
            SET hidden = 1
            WHERE id = ?
              AND itinerary_id IN (
                  SELECT id FROM itinerary WHERE trip_id = ?
              )
            """,
            (item_id, trip_id),
        )
        db.commit()
        db.close()
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/packing", methods=["POST"])
    def update_packing_items(trip_id):
        db = get_db(app)
        rows = db.execute(
            "SELECT id, label FROM packing_items WHERE trip_id = ?",
            (trip_id,),
        ).fetchall()
        for row in rows:
            item_id = row["id"]
            checked = 1 if f"packing_checked_{item_id}" in request.form else 0
            new_label = (request.form.get(f"packing_label_{item_id}") or "").strip()
            if not new_label:
                new_label = row["label"]
            db.execute(
                """
                UPDATE packing_items
                SET checked = ?, label = ?
                WHERE id = ?
                """,
                (checked, new_label, item_id),
            )

        # Handle adding a new packing item, if provided.
        new_label = (request.form.get("new_packing_label") or "").strip()
        new_category = (request.form.get("new_packing_category") or "").strip()
        if new_label:
            if not new_category:
                new_category = "Custom"
            db.execute(
                """
                INSERT INTO packing_items (trip_id, category, label, checked)
                VALUES (?, ?, ?, 0)
                """,
                (trip_id, new_category, new_label),
            )

        db.commit()
        db.close()
        flash("Packing list updated.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/packing/<int:item_id>/delete", methods=["POST"])
    def delete_packing_item(trip_id, item_id):
        db = get_db(app)
        db.execute(
            """
            DELETE FROM packing_items
            WHERE trip_id = ? AND id = ?
            """,
            (trip_id, item_id),
        )
        db.commit()
        db.close()
        flash("Packing item removed.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/suggest-places", methods=["POST"])
    def suggest_places(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()
        days = db.execute(
            "SELECT * FROM itinerary WHERE trip_id = ? ORDER BY day_number",
            (trip_id,),
        ).fetchall()

        if trip is None or not days:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        # Consider multi-location stops so each day can get suggestions
        # tailored to the city/region for that date when available.
        stops = db.execute(
            """
            SELECT name, start_date, end_date
            FROM trip_stops
            WHERE trip_id = ?
            ORDER BY start_date
            """,
            (trip_id,),
        ).fetchall()

        location_by_date: dict[str, str] = {}
        for stop in stops:
            stop_start = parse_date(stop["start_date"])
            stop_end = parse_date(stop["end_date"])
            current = stop_start
            while current <= stop_end:
                location_by_date[current.isoformat()] = stop["name"]
                current += timedelta(days=1)

        # Cache suggestions per distinct location string so we
        # do not call external APIs repeatedly for the same city.
        suggestions_cache: dict[str, dict[str, list[str]]] = {}

        for day in days:
            day_id = day["id"]
            date_str = day["date"]
            location = location_by_date.get(date_str) or trip["destination"]

            cache_key = location
            if cache_key not in suggestions_cache:
                pseudo_trip = {
                    "destination": location,
                    "travel_style": trip["travel_style"],
                }
                suggestions_cache[cache_key] = build_day_suggestions(pseudo_trip)

            day_suggestions = suggestions_cache[cache_key]

            for category, names in day_suggestions.items():
                for name in names:
                    # Avoid exact duplicates for a given day.
                    existing = db.execute(
                        """
                        SELECT 1
                        FROM day_items
                        WHERE itinerary_id = ? AND category = ? AND name = ?
                        """,
                        (day_id, category, name),
                    ).fetchone()
                    if existing:
                        continue
                    db.execute(
                        """
                        INSERT INTO day_items (itinerary_id, category, name, checked)
                        VALUES (?, ?, ?, 0)
                        """,
                        (day_id, category, name),
                    )

        db.commit()
        db.close()
        flash("Sample places, restaurants, and hotels added for each day. Tick the ones you like.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/budget", methods=["POST"])
    def add_budget_item(trip_id):
        label = (request.form.get("label") or "").strip()
        estimated = parse_amount(request.form.get("estimated_cost"))
        actual = parse_amount(request.form.get("actual_cost"))

        if not label:
            flash("Please provide a label for the budget item.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        db = get_db(app)
        db.execute(
            """
            INSERT INTO budget_items (trip_id, label, estimated_cost, actual_cost)
            VALUES (?, ?, ?, ?)
            """,
            (trip_id, label, estimated, actual),
        )
        db.commit()
        db.close()
        flash("Budget item added.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/budget/update", methods=["POST"])
    def update_budget_items(trip_id):
        db = get_db(app)
        items = db.execute(
            """
            SELECT id
            FROM budget_items
            WHERE trip_id = ?
            ORDER BY id
            """,
            (trip_id,),
        ).fetchall()

        for item in items:
            item_id = item["id"]
            raw = request.form.get(f"actual_{item_id}")
            actual = parse_amount(raw)
            db.execute(
                """
                UPDATE budget_items
                SET actual_cost = ?
                WHERE id = ?
                """,
                (actual, item_id),
            )

        db.commit()
        db.close()
        flash("Actual amounts updated.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/budget/<int:item_id>/delete", methods=["POST"])
    def delete_budget_item(trip_id, item_id):
        db = get_db(app)
        db.execute(
            """
            DELETE FROM budget_items
            WHERE trip_id = ? AND id = ?
            """,
            (trip_id, item_id),
        )
        db.commit()
        db.close()
        flash("Budget item removed.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/generate-itinerary", methods=["POST"])
    def generate_itinerary(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()
        days = db.execute(
            "SELECT * FROM itinerary WHERE trip_id = ? ORDER BY day_number",
            (trip_id,),
        ).fetchall()

        if trip is None or not days:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        suggestions = build_ai_style_itinerary(trip, days)
        for day in days:
            content = suggestions[day["day_number"]]

            # Only fill in descriptions that are currently empty so we
            # don't overwrite manual edits or CSV-imported text.
            morning_desc = day["morning_description"] or content["morning_description"]
            afternoon_desc = day["afternoon_description"] or content["afternoon_description"]
            evening_desc = day["evening_description"] or content["evening_description"]

            db.execute(
                """
                UPDATE itinerary
                SET
                    morning_description = ?,
                    afternoon_description = ?,
                    evening_description = ?
                WHERE id = ?
                """,
                (
                    morning_desc,
                    afternoon_desc,
                    evening_desc,
                    day["id"],
                ),
            )

        db.commit()
        db.close()
        flash("Sample itinerary generated based on your travel style.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/recipe")
    def simple_recipe(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()
        db.close()

        if trip is None:
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        recipe = build_simple_local_recipe(trip)
        return render_template("recipe.html", trip=trip, recipe=recipe)

    @app.route("/trip/<int:trip_id>/summary")
    def trip_summary(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()

        if trip is None:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        days = db.execute(
            """
            SELECT *
            FROM itinerary
            WHERE trip_id = ?
            ORDER BY day_number
            """,
            (trip_id,),
        ).fetchall()

        weekday_by_date = {}
        for day in days:
            try:
                date_obj = parse_date(day["date"])
                weekday_by_date[day["date"]] = date_obj.strftime("%A")
            except Exception:
                weekday_by_date[day["date"]] = ""

        stops = db.execute(
            """
            SELECT id, name, start_date, end_date
            FROM trip_stops
            WHERE trip_id = ?
            ORDER BY start_date
            """,
            (trip_id,),
        ).fetchall()

        location_by_date = {}
        for stop in stops:
            stop_start = parse_date(stop["start_date"])
            stop_end = parse_date(stop["end_date"])
            current = stop_start
            while current <= stop_end:
                location_by_date[current.isoformat()] = stop["name"]
                current += timedelta(days=1)

        day_ids = [day["id"] for day in days]
        items_by_day: dict[int, dict[str, list[sqlite3.Row]]] = {}
        if day_ids:
            placeholders = ",".join("?" for _ in day_ids)
            rows = db.execute(
                f"""
                SELECT id, itinerary_id, category, name, checked, slot
                FROM day_items
                WHERE itinerary_id IN ({placeholders})
                  AND (hidden IS NULL OR hidden = 0)
                ORDER BY id
                """,
                day_ids,
            ).fetchall()
            for row in rows:
                items_by_day.setdefault(
                    row["itinerary_id"],
                    {"place": [], "restaurant": [], "hotel": []},
                )
                items_by_day[row["itinerary_id"]][row["category"]].append(row)

        # Build a Google Maps directions URL for each day, when possible.
        directions_by_day: dict[int, str] = {}
        for day in days:
            per_day_items = items_by_day.get(
                day["id"], {"place": [], "restaurant": [], "hotel": []}
            )
            city = location_by_date.get(day["date"]) or trip["destination"]
            url = build_day_directions_url(trip, day, per_day_items, city)
            if url:
                directions_by_day[day["id"]] = url

        db.close()

        return render_template(
            "trip_summary.html",
            trip=trip,
            days=days,
            location_by_date=location_by_date,
            items_by_day=items_by_day,
            weekday_by_date=weekday_by_date,
            directions_by_day=directions_by_day,
        )

    @app.route("/trip/<int:trip_id>/delete", methods=["POST"])
    def delete_trip(trip_id):
        db = get_db(app)

        # Remove day_items linked to this trip's itinerary.
        itinerary_rows = db.execute(
            "SELECT id FROM itinerary WHERE trip_id = ?",
            (trip_id,),
        ).fetchall()
        itinerary_ids = [row["id"] for row in itinerary_rows]
        if itinerary_ids:
            placeholders = ",".join("?" for _ in itinerary_ids)
            db.execute(
                f"DELETE FROM day_items WHERE itinerary_id IN ({placeholders})",
                itinerary_ids,
            )

        # Remove itinerary rows.
        db.execute("DELETE FROM itinerary WHERE trip_id = ?", (trip_id,))

        # Remove budget items and trip stops.
        db.execute("DELETE FROM budget_items WHERE trip_id = ?", (trip_id,))
        db.execute("DELETE FROM trip_stops WHERE trip_id = ?", (trip_id,))

        # Finally, remove the trip itself.
        db.execute("DELETE FROM trips WHERE id = ?", (trip_id,))

        db.commit()
        db.close()
        flash("Trip deleted.")
        return redirect(url_for("list_trips"))

    @app.route("/trip/<int:trip_id>/stops", methods=["POST"])
    def add_trip_stop(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()

        if trip is None:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        name = (request.form.get("name") or "").strip()
        start_str = request.form.get("start_date")
        end_str = request.form.get("end_date")

        if not name or not start_str or not end_str:
            db.close()
            flash("Please provide a city/location and start/end dates for the stop.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        try:
            stop_start = parse_date(start_str)
            stop_end = parse_date(end_str)
            trip_start = parse_date(trip["start_date"])
            trip_end = parse_date(trip["end_date"])
        except ValueError:
            db.close()
            flash("Stop dates must be in YYYY-MM-DD format.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        if stop_end < stop_start:
            db.close()
            flash("Stop end date cannot be before its start date.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        if stop_start < trip_start or stop_end > trip_end:
            db.close()
            flash("Stop dates must fall within the overall trip dates.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        db.execute(
            """
            INSERT INTO trip_stops (trip_id, name, start_date, end_date)
            VALUES (?, ?, ?, ?)
            """,
            (trip_id, name, stop_start.isoformat(), stop_end.isoformat()),
        )
        db.commit()
        db.close()
        flash("Trip stop added.")
        return redirect(url_for("trip_detail", trip_id=trip_id))

    @app.route("/trip/<int:trip_id>/edit", methods=["GET", "POST"])
    def edit_trip(trip_id):
        db = get_db(app)
        trip = db.execute(
            "SELECT * FROM trips WHERE id = ?",
            (trip_id,),
        ).fetchone()

        if trip is None:
            db.close()
            flash("Trip not found.")
            return redirect(url_for("list_trips"))

        if request.method == "POST":
            destination = (request.form.get("destination") or "").strip()
            start_str = request.form.get("start_date")
            end_str = request.form.get("end_date")
            travel_style = request.form.get("travel_style") or "Flexible"

            if not destination or not start_str or not end_str:
                flash("Please provide destination, start date, and end date.")
                return render_template("trip_edit.html", trip=trip)

            try:
                new_start = parse_date(start_str)
                new_end = parse_date(end_str)
            except ValueError:
                flash("Dates must be in YYYY-MM-DD format.")
                return render_template("trip_edit.html", trip=trip)

            if new_end < new_start:
                flash("End date cannot be before start date.")
                return render_template("trip_edit.html", trip=trip)

            old_start = parse_date(trip["start_date"])
            old_end = parse_date(trip["end_date"])

            # Update trip metadata.
            db.execute(
                """
                UPDATE trips
                SET destination = ?, start_date = ?, end_date = ?, travel_style = ?
                WHERE id = ?
                """,
                (destination, new_start.isoformat(), new_end.isoformat(), travel_style, trip_id),
            )

            # Adjust itinerary days if the date range changed.
            if new_start != old_start or new_end != old_end:
                existing_days = db.execute(
                    """
                    SELECT *
                    FROM itinerary
                    WHERE trip_id = ?
                    ORDER BY day_number
                    """,
                    (trip_id,),
                ).fetchall()
                by_date = {row["date"]: row for row in existing_days}
                used_ids = set()

                # Build new sequence of days, reusing any existing day with the same date.
                day_number = 1
                current = new_start
                while current <= new_end:
                    date_str = current.isoformat()
                    if date_str in by_date:
                        row = by_date[date_str]
                        used_ids.add(row["id"])
                        db.execute(
                            """
                            UPDATE itinerary
                            SET day_number = ?, date = ?
                            WHERE id = ?
                            """,
                            (day_number, date_str, row["id"]),
                        )
                    else:
                        db.execute(
                            """
                            INSERT INTO itinerary (trip_id, day_number, date)
                            VALUES (?, ?, ?)
                            """,
                            (trip_id, day_number, date_str),
                        )
                    day_number += 1
                    current += timedelta(days=1)

                # Remove any days that no longer fall within the range,
                # along with their checklist items.
                to_remove = [
                    row["id"]
                    for row in existing_days
                    if row["id"] not in used_ids
                ]
                if to_remove:
                    placeholders = ",".join("?" for _ in to_remove)
                    db.execute(
                        f"DELETE FROM day_items WHERE itinerary_id IN ({placeholders})",
                        to_remove,
                    )
                    db.execute(
                        f"DELETE FROM itinerary WHERE id IN ({placeholders})",
                        to_remove,
                    )

            db.commit()
            db.close()
            flash("Trip updated.")
            return redirect(url_for("trip_detail", trip_id=trip_id))

        db.close()
        return render_template("trip_edit.html", trip=trip)

    return app


def get_db(app: Flask) -> sqlite3.Connection:
    conn = sqlite3.connect(app.config["DATABASE"])
    conn.row_factory = sqlite3.Row
    return conn


def ensure_database(app: Flask) -> None:
    os.makedirs(app.root_path, exist_ok=True)
    conn = get_db(app)
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS trips (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            destination TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            travel_style TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS itinerary (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            day_number INTEGER NOT NULL,
            date TEXT NOT NULL,
            morning_title TEXT,
            morning_description TEXT,
            morning_map_link TEXT,
            afternoon_title TEXT,
            afternoon_description TEXT,
            afternoon_map_link TEXT,
            evening_title TEXT,
            evening_description TEXT,
            evening_map_link TEXT,
            FOREIGN KEY (trip_id) REFERENCES trips (id)
        );

        CREATE TABLE IF NOT EXISTS budget_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            label TEXT NOT NULL,
            estimated_cost REAL DEFAULT 0,
            actual_cost REAL DEFAULT 0,
            FOREIGN KEY (trip_id) REFERENCES trips (id)
        );

        CREATE TABLE IF NOT EXISTS day_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            itinerary_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            name TEXT NOT NULL,
            checked INTEGER NOT NULL DEFAULT 0,
            slot TEXT,
            hidden INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (itinerary_id) REFERENCES itinerary (id)
        );

        CREATE TABLE IF NOT EXISTS trip_stops (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            name TEXT NOT NULL,
            start_date TEXT NOT NULL,
            end_date TEXT NOT NULL,
            FOREIGN KEY (trip_id) REFERENCES trips (id)
        );

        CREATE TABLE IF NOT EXISTS packing_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            label TEXT NOT NULL,
            checked INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (trip_id) REFERENCES trips (id)
        );
        """
    )
    # Lightweight migrations for existing databases.
    try:
        columns = conn.execute("PRAGMA table_info(day_items)").fetchall()
        column_names = {col["name"] for col in columns}
        if "slot" not in column_names:
            conn.execute("ALTER TABLE day_items ADD COLUMN slot TEXT")
        if "hidden" not in column_names:
            conn.execute("ALTER TABLE day_items ADD COLUMN hidden INTEGER NOT NULL DEFAULT 0")
    except sqlite3.DatabaseError:
        # If anything goes wrong, ignore; the app will simply not use slots.
        pass
    # Ensure packing_items exists even if the initial execscript failed for any reason.
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS packing_items (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            trip_id INTEGER NOT NULL,
            category TEXT NOT NULL,
            label TEXT NOT NULL,
            checked INTEGER NOT NULL DEFAULT 0,
            FOREIGN KEY (trip_id) REFERENCES trips (id)
        )
        """
    )
    conn.commit()
    conn.close()


def parse_date(value: str) -> datetime.date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def parse_amount(value: str | None) -> float:
    if not value:
        return 0.0
    try:
        return float(value)
    except ValueError:
        return 0.0


def build_ai_style_itinerary(trip, days):
    destination = trip["destination"]
    style = (trip["travel_style"] or "").lower()

    if "food" in style or "foodie" in style:
        morning = f"Start your day at a local cafe in {destination}."
        afternoon = f"Take a walking food tour and sample street food in {destination}."
        evening = f"Dinner at a neighborhood restaurant known for regional specialties in {destination}."
    elif "budget" in style:
        morning = f"Explore a free park, garden, or public space in {destination}."
        afternoon = f"Visit a low-cost museum or market and people-watch in {destination}."
        evening = f"Find a casual, affordable spot for dinner and stroll the city center in {destination}."
    elif "family" in style:
        morning = f"Family-friendly attraction or playground in {destination}."
        afternoon = f"Interactive museum, zoo, or aquarium suited for kids in {destination}."
        evening = f"Relaxed dinner and early evening walk through a safe, lively area in {destination}."
    elif "luxury" in style:
        morning = f"Slow breakfast at a high-end cafe or in-hotel dining in {destination}."
        afternoon = f"Spa treatment or private guided tour around {destination}."
        evening = f"Tasting-menu dinner or rooftop bar experience in {destination}."
    else:
        morning = f"Walk through a central neighborhood in {destination}."
        afternoon = f"Visit one landmark or museum that interests you in {destination}."
        evening = f"Try a recommended local restaurant and explore nearby streets in {destination}."

    itinerary = {}
    for day in days:
        day_number = day["day_number"]
        itinerary[day_number] = {
            "morning_title": "Morning",
            "morning_description": morning,
            "afternoon_title": "Afternoon",
            "afternoon_description": afternoon,
            "evening_title": "Evening",
            "evening_description": evening,
        }

    return itinerary


def build_packing_list(trip, days):
    destination = (trip["destination"] or "").lower()
    style = (trip["travel_style"] or "").lower()

    items = {"Essentials": ["Passport / ID", "Phone + charger", "Wallet + cards", "Medications"]}

    if any("rain" in d["date"] for d in days):  # placeholder; dates themselves do not contain rain info
        pass

    if "seattle" in destination or "rain" in destination:
        items.setdefault("Weather", []).append("Light raincoat or waterproof jacket")

    first_date = parse_date(trip["start_date"])
    if first_date.month in (12, 1, 2):
        items.setdefault("Clothing", []).append("Warm layers and a hat")
    elif first_date.month in (6, 7, 8):
        items.setdefault("Clothing", []).append("Lightweight clothing and sunscreen")

    if "food" in style or "foodie" in style:
        items.setdefault("Foodie Tools", []).extend(["Reusable tote bag for markets", "Small notebook for food notes"])

    if "beach" in destination or "island" in destination:
        items.setdefault("Activities", []).extend(["Swimsuit", "Flip-flops", "Beach towel"])

    return items


def get_packing_items_for_trip(db: sqlite3.Connection, trip, days):
    """
    Ensure packing_items rows exist for this trip based on the suggested
    packing list, and return them grouped by category.
    """
    trip_id = trip["id"]
    suggested = build_packing_list(trip, days)

    existing_rows = db.execute(
        """
        SELECT id, category, label, checked
        FROM packing_items
        WHERE trip_id = ?
        """,
        (trip_id,),
    ).fetchall()
    existing_by_key = {(row["category"], row["label"]): row for row in existing_rows}

    # Seed any missing suggested items.
    for category, labels in suggested.items():
        for label in labels:
            key = (category, label)
            if key not in existing_by_key:
                db.execute(
                    """
                    INSERT INTO packing_items (trip_id, category, label, checked)
                    VALUES (?, ?, ?, 0)
                    """,
                    (trip_id, category, label),
                )

    # Reload and group.
    rows = db.execute(
        """
        SELECT id, category, label, checked
        FROM packing_items
        WHERE trip_id = ?
        ORDER BY category, label
        """,
        (trip_id,),
    ).fetchall()

    by_category: dict[str, list[sqlite3.Row]] = {}
    for row in rows:
        by_category.setdefault(row["category"], []).append(row)
    return by_category


def build_weather_stub(trip, days):
    """
    Return a simple 7-day forecast.

    If OPENWEATHER_API_KEY is set, this uses the OpenWeather
    One Call / Geo APIs; otherwise it falls back to a basic
    offline placeholder forecast.
    """
    api_key = os.environ.get("OPENWEATHER_API_KEY")
    if not api_key:
        return _offline_weather_forecast(trip, days)

    try:
        forecast = _build_weather_from_openweather(trip, days, api_key)
        if forecast:
            return forecast
    except Exception:
        # Keep the app usable even if the API fails.
        pass

    return _offline_weather_forecast(trip, days)


def _offline_foodie_highlights(trip):
    destination = (trip["destination"] or "").lower()
    highlights = {
        "dishes_to_try": [],
        "hidden_gems": [],
        "grocery_list": [],
    }

    if "charleston" in destination:
        highlights["dishes_to_try"] = ["Shrimp & grits", "She-crab soup", "Fried green tomatoes"]
        highlights["hidden_gems"] = ["Local shrimp shack away from the main tourist strip"]
        highlights["grocery_list"] = ["Fresh shrimp", "Grits", "Butter", "Garlic"]
    elif "tokyo" in destination:
        highlights["dishes_to_try"] = ["Tsukemen ramen", "Tonkatsu", "Matcha dessert"]
        highlights["hidden_gems"] = ["Standing sushi bar near a train station", "Tiny ramen shop with 10 seats"]
        highlights["grocery_list"] = ["Rice", "Soy sauce", "Miso paste", "Seasonal vegetables"]
    else:
        highlights["dishes_to_try"] = [f"One hearty local dish in {trip['destination']}"]
        highlights["hidden_gems"] = ["Ask a local barista or waiter where they eat on their day off."]
        highlights["grocery_list"] = ["3 seasonal vegetables", "Local cheese or protein", "Bread or rice"]

    return highlights


def build_foodie_highlights(trip):
    """
    Build the Foodie Twist section.

    When GOOGLE_PLACES_API_KEY is set, this will use the same
    Google Places-powered suggestion logic used for day
    recommendations to surface real restaurants and places.
    Otherwise it falls back to simple offline text.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return _offline_foodie_highlights(trip)

    try:
        suggestions = _build_day_suggestions_from_google_places(trip, api_key)
        restaurants = suggestions.get("restaurant") or []
        places = suggestions.get("place") or []

        dishes: list[str] = []
        for entry in restaurants:
            # Entries are "Name (Address)" – trim to just the name.
            name = entry.split("(", 1)[0].strip()
            if name and name not in dishes:
                dishes.append(name)

        hidden: list[str] = []
        for entry in places:
            name = entry.split("(", 1)[0].strip()
            if name and name not in hidden:
                hidden.append(name)

        highlights = _offline_foodie_highlights(trip)

        if dishes:
            highlights["dishes_to_try"] = dishes
        if hidden:
            highlights["hidden_gems"] = hidden

        if not highlights.get("grocery_list"):
            highlights["grocery_list"] = [
                "Fresh fruit or local snacks",
                "Bottled water or drinks",
                "Breakfast basics for the room",
            ]

        return highlights
    except Exception:
        # Keep the app usable if anything goes wrong with Places.
        return _offline_foodie_highlights(trip)


def build_simple_local_recipe(trip):
    destination = (trip["destination"] or "").lower()

    if "charleston" in destination:
        title = "Ultra-Simple Shrimp & Grits"
        description = "A one-pan, low-stress version of a Charleston classic you can cook in most Airbnbs."
        ingredients = [
            "Frozen or fresh peeled shrimp",
            "Quick-cooking grits",
            "Butter or olive oil",
            "Garlic (fresh or pre-minced)",
            "Salt and pepper",
        ]
        steps = [
            "Cook grits according to the package in a small pot with water and a spoonful of butter.",
            "While grits cook, heat a pan with a little butter or oil and gently cook garlic for 30–60 seconds.",
            "Add shrimp to the pan, season with salt and pepper, and cook until pink on both sides.",
            "Spoon grits into a bowl and top with the garlic shrimp and pan juices.",
        ]
    elif "tokyo" in destination:
        title = "Lazy Tokyo Rice Bowl"
        description = "A 10–15 minute rice bowl using simple ingredients from a Japanese convenience store or small market."
        ingredients = [
            "Cooked rice (microwaveable pack is fine)",
            "Soy sauce",
            "Green onions or any soft vegetable",
            "Egg (or tofu if you prefer)",
        ]
        steps = [
            "Heat the rice according to the package and place it in a bowl.",
            "Gently fry an egg sunny-side-up (or warm cubed tofu) in a little oil.",
            "Slice green onions or soft vegetables into small pieces.",
            "Top the rice with the egg or tofu, sprinkle over the vegetables, and drizzle soy sauce to taste.",
        ]
    else:
        title = "One-Pan Local Veggie Toast"
        description = "A flexible, low-skill meal you can make almost anywhere with just a pan and toaster."
        ingredients = [
            "Good bread or rolls",
            "Local cheese or spread",
            "2–3 local vegetables (tomato, peppers, greens, etc.)",
            "Olive oil or butter",
            "Salt and pepper",
        ]
        steps = [
            "Toast the bread or warm it in a pan until lightly crisp.",
            "Slice the vegetables into bite-sized pieces.",
            "Gently sauté the vegetables in a little oil or butter until just soft; season with salt and pepper.",
            "Spread cheese on the warm bread and pile the cooked vegetables on top.",
        ]

    return {
        "title": title,
        "description": description,
        "ingredients": ingredients,
        "steps": steps,
    }


def _offline_weather_forecast(trip, days):
    # Very simple placeholder forecast used when there is
    # no API key or the API call fails.
    return [
        {
            "date": day["date"],
            "summary": "Forecast placeholder (connect to real API later)",
        }
        for day in days[:7]
    ]


def _build_weather_from_openweather(trip, days, api_key: str):
    """
    Use OpenWeather's Geo + 5-day forecast APIs to get a daily summary.

    This assumes OPENWEATHER_API_KEY is configured in the environment.
    """
    destination = trip["destination"]

    # First, geocode the destination to lat/lon.
    geo_resp = requests.get(
        "https://api.openweathermap.org/geo/1.0/direct",
        params={"q": destination, "limit": 1, "appid": api_key},
        timeout=8,
    )
    geo_resp.raise_for_status()
    geo_data = geo_resp.json()
    if not geo_data:
        return _offline_weather_forecast(trip, days)

    lat = geo_data[0].get("lat")
    lon = geo_data[0].get("lon")
    if lat is None or lon is None:
        return _offline_weather_forecast(trip, days)

    # Then, fetch a 5-day / 3-hour forecast and aggregate by date.
    weather_resp = requests.get(
        "https://api.openweathermap.org/data/2.5/forecast",
        params={
            "lat": lat,
            "lon": lon,
            "units": "metric",
            "appid": api_key,
        },
        timeout=8,
    )
    weather_resp.raise_for_status()
    data = weather_resp.json()
    blocks = data.get("list") or []

    by_date: dict[str, dict[str, list]] = {}
    for entry in blocks:
        dt = entry.get("dt")
        if not dt:
            continue
        date_str = datetime.utcfromtimestamp(dt).date().isoformat()
        main = entry.get("main") or {}
        temp = main.get("temp")
        weather_list = entry.get("weather") or []
        description = ""
        if weather_list:
            description = (weather_list[0].get("description") or "").capitalize()

        bucket = by_date.setdefault(date_str, {"temps": [], "descriptions": []})
        if temp is not None:
            bucket["temps"].append(temp)
        if description:
            bucket["descriptions"].append(description)

    summaries: dict[str, str] = {}
    for date_str, bucket in by_date.items():
        temps = bucket["temps"]
        descriptions = bucket["descriptions"]
        parts = []
        if descriptions:
            parts.append(descriptions[0])
        if temps:
            temp_min = round(min(temps))
            temp_max = round(max(temps))
            parts.append(f"{temp_max}° / {temp_min}°C")
        summary = " · ".join(parts) if parts else "Forecast unavailable"
        summaries[date_str] = summary

    forecast = []
    for day in days[:7]:
        date_str = day["date"]
        summary = summaries.get(date_str, "Forecast unavailable")
        forecast.append({"date": date_str, "summary": summary})

    return forecast


def _offline_day_suggestions(trip):
    destination = (trip["destination"] or "").lower()

    suggestions = {
        "place": [],
        "restaurant": [],
        "hotel": [],
    }

    if "charleston" in destination:
        suggestions["place"] = [
            "Rainbow Row & historic downtown walk",
            "Waterfront Park & Pineapple Fountain",
        ]
        suggestions["restaurant"] = [
            "Husk (modern Southern)",
            "Fleet Landing Restaurant & Bar (waterfront seafood)",
        ]
        suggestions["hotel"] = [
            "The Dewberry Charleston",
            "Emeline",
        ]
    elif "tokyo" in destination:
        suggestions["place"] = [
            "Senso-ji Temple in Asakusa",
            "Meiji Shrine & Harajuku walk",
            "Shibuya Crossing evening stroll",
        ]
        suggestions["restaurant"] = [
            "Ichiran Ramen (solo ramen booths)",
            "Gyukatsu Motomura (beef cutlet)",
        ]
        suggestions["hotel"] = [
            "Hotel Niwa Tokyo",
            "Shinjuku Granbell Hotel",
        ]
    else:
        suggestions["place"] = [
            f"Old town or historic center in {trip['destination']}",
            f"City park or viewpoint in {trip['destination']}",
        ]
        suggestions["restaurant"] = [
            f"Highly rated casual restaurant near your stay in {trip['destination']}",
            f"Bakery or cafe popular with locals in {trip['destination']}",
        ]
        suggestions["hotel"] = [
            f"Mid-range hotel close to transit in {trip['destination']}",
            f"Guesthouse or small inn with good reviews in {trip['destination']}",
        ]

    return suggestions


def _build_day_suggestions_from_google_places(trip, api_key: str):
    destination = trip["destination"]
    style = (trip["travel_style"] or "").lower()

    base_url = "https://maps.googleapis.com/maps/api/place/textsearch/json"

    def search(query: str, place_type: str | None = None, limit: int = 5):
        params = {"query": query, "key": api_key}
        if place_type:
            params["type"] = place_type
        response = requests.get(base_url, params=params, timeout=8)
        response.raise_for_status()
        data = response.json()
        status = data.get("status")
        if status not in ("OK", "ZERO_RESULTS"):
            return []
        results = data.get("results", [])[:limit]
        names: list[str] = []
        for item in results:
            name = item.get("name")
            address = item.get("formatted_address") or item.get("vicinity")
            if name and address:
                names.append(f"{name} ({address})")
            elif name:
                names.append(name)
        return names

    places = search(f"tourist attractions in {destination}", place_type="tourist_attraction", limit=5)

    restaurant_phrase = "restaurants"
    if "budget" in style:
        restaurant_phrase = "cheap eats"
    elif "luxury" in style:
        restaurant_phrase = "fine dining restaurants"
    elif "family" in style:
        restaurant_phrase = "family friendly restaurants"
    elif "food" in style or "foodie" in style:
        restaurant_phrase = "best local food"
    restaurants = search(f"{restaurant_phrase} in {destination}", place_type="restaurant", limit=5)

    hotel_phrase = "hotels"
    if "budget" in style:
        hotel_phrase = "budget hotels"
    elif "luxury" in style:
        hotel_phrase = "luxury hotels"
    hotels = search(f"{hotel_phrase} in {destination}", place_type="lodging", limit=5)

    suggestions = {
        "place": places,
        "restaurant": restaurants,
        "hotel": hotels,
    }

    # Fallback on offline suggestions for any empty category.
    if not all(suggestions.values()):
        offline = _offline_day_suggestions(trip)
        for key in ("place", "restaurant", "hotel"):
            if not suggestions[key]:
                suggestions[key] = offline[key]

    return suggestions


def build_day_suggestions(trip):
    """
    Return suggestions for places, restaurants, and hotels.

    If the environment variable GOOGLE_PLACES_API_KEY is set,
    this uses the Google Places Text Search API; otherwise it
    falls back to simple offline suggestions.
    """
    api_key = os.environ.get("GOOGLE_PLACES_API_KEY")
    if not api_key:
        return _offline_day_suggestions(trip)

    try:
        return _build_day_suggestions_from_google_places(trip, api_key)
    except Exception:
        # On any network or API error, keep the app usable.
        return _offline_day_suggestions(trip)


if __name__ == "__main__":
    app = create_app()
    port = int(os.environ.get("PORT", "5000"))
    app.run(host="0.0.0.0", port=port, debug=True)
