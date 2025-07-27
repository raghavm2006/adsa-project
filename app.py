from flask import Flask, render_template, request, redirect, url_for, session, flash, jsonify, send_from_directory
import pyrebase
import os
import requests
import logging
from flexpolyline import decode
from heapq import heappush, heappop
from math import sqrt
import networkx as nx

# Configure logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.secret_key = os.urandom(24)

# Firebase Configuration
firebase_config = {
    "apiKey": "AIzaSyAdKsHhbCgvoRHd2wpe636lroB01VmKiB0",
    "databaseURL": "https://traffic-prediction-90c31-default-rtdb.firebaseio.com/",
    "authDomain": "traffic-prediction-90c31.firebaseapp.com",
    "projectId": "traffic-prediction-90c31",
    "storageBucket": "traffic-prediction-90c31.firebasestorage.app",
    "messagingSenderId": "712834657117",
    "appId": "1:712834657117:web:e9d2d8732f6e3cd6c616b4",
    "measurementId": "G-EY95ENTQM6"
}

firebase = pyrebase.initialize_app(firebase_config)
auth = firebase.auth()

# HERE API Configuration
HERE_API_KEY = "vfkEAl_HJiKREUeiUk83zPdt3EotBKqCW_v1kOrvwdw"
geocode_cache = {}

# Helper functions (unchanged from previous code)
def get_coordinates(location_name):
    if isinstance(location_name, (tuple, list)):
        return tuple(location_name)
    if location_name in geocode_cache:
        logger.debug(f"Using cached coordinates for {location_name}: {geocode_cache[location_name]}")
        return geocode_cache[location_name]
    geocode_url = "https://geocode.search.hereapi.com/v1/geocode"
    params = {"q": location_name, "apiKey": HERE_API_KEY}
    logger.debug(f"Geocoding request for: {location_name}")
    response = requests.get(geocode_url, params=params)
    logger.debug(f"Geocode response status: {response.status_code}, content: {response.text}")
    response_data = response.json()
    if response_data.get("items"):
        lat, lng = response_data["items"][0]["position"]["lat"], response_data["items"][0]["position"]["lng"]
        geocode_cache[location_name] = (lat, lng)
        logger.debug(f"Geocoded coordinates: ({lat}, {lng})")
        return lat, lng
    logger.error(f"Failed to geocode {location_name}")
    return None

def get_traffic_data(source, destination):
    if not (isinstance(source, tuple) and isinstance(destination, tuple)):
        logger.error(f"Invalid coordinates for traffic data: source={source}, destination={destination}")
        return {}, "No Traffic"
    route_distance = sqrt((source[0] - destination[0]) ** 2 + (source[1] - destination[1]) ** 2) * 111
    radius = max(2000, min(int(route_distance * 1000 * 0.3), 10000))
    logger.debug(f"Calculated route distance: {route_distance} km, radius: {radius} m")
    mid_lat = (source[0] + destination[0]) / 2
    mid_lon = (source[1] + destination[1]) / 2
    traffic_url = "https://data.traffic.hereapi.com/v7/flow"
    params = {"apiKey": HERE_API_KEY, "in": f"circle:{mid_lat},{mid_lon};r={radius}", "locationReferencing": "olr"}
    logger.debug(f"Traffic API request params: {params}")
    response = requests.get(traffic_url, params=params)
    logger.debug(f"Traffic response status: {response.status_code}, content: {response.text}")
    traffic_data = response.json()
    traffic_segments = {}
    total_jam_factor = 0
    segment_count = 0
    for segment in traffic_data.get("results", []):
        road_name = segment["location"].get("description", "Unknown Road")
        current_flow = segment.get("currentFlow", {})
        jam_factor = current_flow.get("jamFactor", 0)
        current_speed = current_flow.get("speed", 0) or 50
        free_flow = current_flow.get("freeFlow", 0)
        free_flow_speed = free_flow if isinstance(free_flow, (int, float)) else free_flow.get("speed", 50) or 50
        delay = (free_flow_speed / current_speed - 1) * 60 if current_speed > 0 and current_speed < free_flow_speed else 0
        traffic_segments[road_name] = {"jam_factor": jam_factor, "speed": current_speed, "free_flow_speed": free_flow_speed, "delay": delay}
        total_jam_factor += jam_factor
        segment_count += 1
        logger.debug(f"Segment {road_name}: jam_factor={jam_factor}, speed={current_speed}, free_flow_speed={free_flow_speed}")
    avg_jam_factor = total_jam_factor / max(1, segment_count) if segment_count else 0
    logger.debug(f"Traffic segments: {traffic_segments}, avg jam factor: {avg_jam_factor}")
    if segment_count == 0 or avg_jam_factor <= 2:
        route_url = "https://router.hereapi.com/v8/routes"
        params = {"apiKey": HERE_API_KEY, "transportMode": "car", "origin": f"{source[0]},{source[1]}", "destination": f"{destination[0]},{destination[1]}", "return": "travelSummary"}
        route_response = requests.get(route_url, params=params)
        logger.debug(f"Routing API fallback response status: {route_response.status_code}, content: {route_response.text}")
        route_data = route_response.json()
        if "routes" in route_data and route_data["routes"]:
            traffic_delay = route_data["routes"][0]["sections"][0]["travelSummary"].get("trafficDelay", 0)
            logger.debug(f"Fallback traffic delay: {traffic_delay} seconds")
            if traffic_delay > 300:
                traffic_category = "Heavy"
            elif traffic_delay > 120:
                traffic_category = "Medium"
            elif traffic_delay > 0:
                traffic_category = "Light"
            else:
                traffic_category = "No Traffic"
        else:
            traffic_category = "No Traffic"
    else:
        if avg_jam_factor <= 2:
            traffic_category = "No Traffic"
        elif avg_jam_factor <= 5:
            traffic_category = "Light"
        elif avg_jam_factor <= 8:
            traffic_category = "Medium"
        else:
            traffic_category = "Heavy"
    logger.debug(f"Final traffic category: {traffic_category}")
    return traffic_segments, traffic_category

def get_alternate_routes(source, destination, waypoints, transport_mode):
    if not (isinstance(source, tuple) and isinstance(destination, tuple)):
        logger.error(f"Invalid coordinates: source={source}, destination={destination}")
        return None
    route_url = "https://router.hereapi.com/v8/routes"
    params = {"apiKey": HERE_API_KEY, "transportMode": transport_mode, "origin": f"{source[0]},{source[1]}", "destination": f"{destination[0]},{destination[1]}", "alternatives": 3, "return": "routeLabels,summary,polyline,travelSummary", "lang": "en-gb"}
    if waypoints and len(waypoints) > 0:
        via_points = [get_coordinates(wp) for wp in waypoints if get_coordinates(wp)]
        if via_points:
            params["via"] = ";".join([f"{lat},{lng}" for lat, lng in via_points])
            logger.debug(f"Added via points: {via_points}")
    logger.debug(f"Routing API request params: {params}")
    route_response = requests.get(route_url, params=params)
    logger.debug(f"Routing response status: {route_response.status_code}, content: {route_response.text}")
    route_data = route_response.json()
    if "routes" not in route_data or not route_data["routes"]:
        logger.error("No routes found in response")
        return None
    num_routes = len(route_data["routes"])
    logger.debug(f"Found {num_routes} routes")
    return route_data["routes"]

def create_graph_from_routes(routes, source, destination):
    if not routes:
        logger.error("No routes provided to create graph")
        return None, []
    traffic_segments, traffic_category = get_traffic_data(source, destination)
    waypoints_graph = nx.Graph()
    all_waypoints = set()
    route_details = []
    for route_idx, route in enumerate(routes):
        logger.debug(f"Processing route {route_idx + 1}")
        labels = [label["name"]["value"] for label in route.get("routeLabels", [])]
        polyline = route["sections"][0]["polyline"]
        coordinates = decode(polyline)
        coordinates = [(lat, lon) for lat, lon, *_ in coordinates]
        duration = route["sections"][0]["travelSummary"]["duration"]
        traffic_delay = route["sections"][0]["travelSummary"].get("trafficDelay", 0)
        total_time = duration + traffic_delay
        route_details.append({"index": route_idx, "time": total_time, "coordinates": coordinates, "labels": labels})
        for j in range(len(coordinates) - 1):
            coord1, coord2 = coordinates[j], coordinates[j + 1]
            all_waypoints.add(coord1)
            all_waypoints.add(coord2)
            distance = sqrt((coord1[0] - coord2[0]) ** 2 + (coord1[1] - coord2[1]) ** 2)
            segment_delay = 0 if traffic_category == "No Traffic" else 60 if traffic_category == "Light" else 120 if traffic_category == "Medium" else 300
            time_weight = distance + segment_delay
            waypoints_graph.add_edge(coord1, coord2, weight=time_weight, distance=distance)
            logger.debug(f"Added edge {coord1} -> {coord2}, weight: {time_weight}")
    waypoints_graph.add_node(source)
    waypoints_graph.add_node(destination)
    all_waypoints.add(source)
    all_waypoints.add(destination)
    other_waypoints = all_waypoints - {source, destination}
    if not other_waypoints:
        logger.error("No waypoints available to connect source and destination")
        return None, route_details
    def find_nearest_waypoint(target, waypoints):
        return min(waypoints, key=lambda w: sqrt((w[0] - target[0]) ** 2 + (w[1] - target[1]) ** 2))
    nearest_to_source = find_nearest_waypoint(source, other_waypoints)
    nearest_to_dest = find_nearest_waypoint(destination, other_waypoints)
    dist_to_source = sqrt((source[0] - nearest_to_source[0]) ** 2 + (source[1] - nearest_to_source[1]) ** 2)
    dist_to_dest = sqrt((destination[0] - nearest_to_dest[0]) ** 2 + (destination[1] - nearest_to_dest[1]) ** 2)
    waypoints_graph.add_edge(source, nearest_to_source, weight=dist_to_source, distance=dist_to_source)
    waypoints_graph.add_edge(destination, nearest_to_dest, weight=dist_to_dest, distance=dist_to_dest)
    logger.debug(f"Connected source {source} to {nearest_to_source} and destination {destination} to {nearest_to_dest}")
    logger.debug(f"Graph nodes: {list(waypoints_graph.nodes)}")
    logger.debug(f"Graph edges: {list(waypoints_graph.edges(data=True))}")
    return waypoints_graph, route_details

def dijkstra(graph, start, end):
    if start not in graph.nodes:
        logger.error(f"Start node {start} not in graph")
        return []
    if end not in graph.nodes:
        logger.error(f"End node {end} not in graph")
        return []
    distances = {node: float('inf') for node in graph.nodes}
    distances[start] = 0
    previous = {node: None for node in graph.nodes}
    pq = [(0, start)]
    while pq:
        current_distance, current_node = heappop(pq)
        if current_node == end:
            break
        if current_distance > distances[current_node]:
            continue
        for neighbor in graph.neighbors(current_node):
            weight = graph[current_node][neighbor]["weight"]
            distance = current_distance + weight
            if distance < distances[neighbor]:
                distances[neighbor] = distance
                previous[neighbor] = current_node
                heappush(pq, (distance, neighbor))
                logger.debug(f"Updated distance to {neighbor}: {distance}")
    path = []
    current_node = end
    while current_node is not None:
        path.append(current_node)
        current_node = previous.get(current_node)
        if current_node == start:
            path.append(current_node)
            break
    path = path[::-1]
    if not path or path[0] != start or path[-1] != end:
        logger.error(f"Dijkstra failed to find a valid path from {start} to {end}")
        return []
    logger.debug(f"Dijkstra path from {start} to {end}: {path}")
    return path

def get_best_route_geojson(source, destination, waypoints, transport_mode):
    logger.debug(f"Getting best route: source={source}, dest={destination}, waypoints={waypoints}, mode={transport_mode}")
    source_coords = get_coordinates(source)
    dest_coords = get_coordinates(destination)
    if not source_coords or not dest_coords:
        logger.error("Failed to get coordinates for source or destination")
        raise ValueError("Invalid source or destination coordinates")
    routes = get_alternate_routes(source_coords, dest_coords, waypoints, transport_mode)
    if not routes:
        logger.error("Failed to get alternate routes")
        raise ValueError("No alternate routes found")
    G, route_details = create_graph_from_routes(routes, source_coords, dest_coords)
    if not G:
        logger.error("Failed to create graph")
        raise ValueError("Failed to create route graph")
    shortest_path = dijkstra(G, source_coords, dest_coords)
    if not shortest_path:
        logger.error("No shortest path found")
        raise ValueError("No shortest path found")
    traffic_segments, traffic_category = get_traffic_data(source_coords, dest_coords)
    via_coords = [get_coordinates(wp) for wp in waypoints if get_coordinates(wp)]
    for via in via_coords:
        if via not in G.nodes:
            G.add_node(via)
            nearest = min(G.nodes - {via}, key=lambda w: sqrt((w[0] - via[0]) ** 2 + (w[1] - via[1]) ** 2))
            dist = sqrt((via[0] - nearest[0]) ** 2 + (via[1] - nearest[1]) ** 2)
            G.add_edge(via, nearest, weight=dist, distance=dist)
            logger.debug(f"Connected waypoint {via} to {nearest}")
    all_coords = [source_coords] + via_coords + [dest_coords]
    forced_path = []
    for i in range(len(all_coords) - 1):
        start = all_coords[i]
        end = all_coords[i + 1]
        sub_path = dijkstra(G, start, end)
        if not sub_path:
            logger.warning(f"No path found between {start} and {end}, falling back to shortest_path")
            forced_path = shortest_path
            break
        if i > 0:
            forced_path.extend(sub_path[1:])
        else:
            forced_path.extend(sub_path)
    if len(forced_path) < 2 or forced_path[0] != source_coords or forced_path[-1] != dest_coords:
        logger.warning("Forced path invalid or incomplete, reconstructing with all waypoints")
        forced_path = [source_coords]
        for via in via_coords:
            forced_path.append(via)
        forced_path.append(dest_coords)
    coordinates = [[coord[1], coord[0]] for coord in forced_path]
    geojson = {
        "type": "FeatureCollection",
        "features": [{
            "type": "Feature",
            "geometry": {
                "type": "LineString",
                "coordinates": coordinates
            },
            "properties": {
                "name": "Best Route",
                "traffic_category": traffic_category
            }
        }]
    }
    logger.debug(f"Returning GeoJSON with {len(coordinates)} points: {coordinates[:5]}...")
    return geojson

# Routes
@app.route("/")
def home():
    logger.debug("Accessing home route")
    return render_template("home.html")

@app.route("/signup", methods=["GET", "POST"])
def signup():
    logger.debug("Accessing signup route")
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        try:
            auth.create_user_with_email_and_password(email, password)
            flash("Signup successful! Please login.", "success")
            return redirect(url_for("login"))
        except Exception as e:
            flash("Error: " + str(e), "danger")
    return render_template("signup.html")

@app.route("/login", methods=["GET", "POST"])
def login():
    logger.debug("Accessing login route")
    if request.method == "POST":
        email = request.form["email"]
        password = request.form["password"]
        try:
            user = auth.sign_in_with_email_and_password(email, password)
            session["user"] = user["idToken"]  # Store the ID token in session
            logger.debug(f"Login successful, redirecting to /index. Session: {session}")
            flash("Login successful!", "success")
            return redirect(url_for("index"))  # Redirect to map page
        except Exception as e:
            flash("Invalid Credentials!", "danger")
    return render_template("login.html")

@app.route("/dashboard")
def dashboard():
    logger.debug("Accessing dashboard route. Session: {session}")
    if "user" in session:
        logger.debug("Redirecting from dashboard to index for authenticated user")
        return redirect(url_for("index"))
    else:
        flash("Please login first!", "warning")
        return redirect(url_for("login"))

@app.route("/logout")
def logout():
    logger.debug("Accessing logout route")
    session.pop("user", None)
    flash("Logged out successfully!", "info")
    return redirect(url_for("home"))

@app.route("/index")
def index():
    logger.debug(f"Accessing index route. Session: {session}")
    if "user" in session:
        logger.debug("User authenticated, serving static/index.html")
        return send_from_directory("static", "index.html")
    else:
        flash("Please login to access the map!", "warning")
        return redirect(url_for("login"))

@app.route("/route", methods=["POST"])
def route():
    logger.debug(f"Accessing route route. Session: {session}")
    if "user" in session:
        try:
            data = request.json
            logger.debug(f"Received route request: {data}")
            source = data.get("source")
            destination = data.get("destination")
            last_center = data.get("last_center")
            last_zoom = data.get("last_zoom")
            transport_mode = data.get("transportMode", "car")
            waypoints = data.get("waypoints", [])
            if isinstance(source, list):
                source = tuple(source)
            geojson = get_best_route_geojson(source, destination, waypoints, transport_mode)
            return jsonify({"geojson": geojson})
        except Exception as e:
            logger.error(f"Error processing route request: {str(e)}")
            return jsonify({"error": str(e)}), 500
    else:
        return jsonify({"error": "Unauthorized: Please login first"}), 401

@app.route('/check_session')
def check_session():
    logger.debug(f"Checking session: {session}")
    if "user" in session:
        return jsonify({"status": "authenticated"})
    else:
        return jsonify({"error": "Not authenticated"}), 401

# New routes for password reset
@app.route('/forgot_password', methods=['GET', 'POST'])
def forgot_password():
    logger.debug("Accessing forgot_password route")
    if request.method == 'POST':
        email = request.form['email']
        try:
            auth.send_password_reset_email(email)
            flash("Password reset link sent to your email!", "success")
            return redirect(url_for('login'))
        except Exception as e:
            flash(f"Error: {str(e)}", "danger")
    return render_template('forgot.html')

@app.route('/reset_password', methods=['GET', 'POST'])
def reset_password():
    logger.debug("Accessing reset_password route")
    if request.method == 'POST':
        new_password = request.form['new_password']
        # Note: In a real implementation, you would need the oobCode from the reset link
        # For simplicity, we'll assume the user is redirected here with a valid session or token
        # This is a simplified version; a production app would verify the oobCode
        if 'user' in session:
            try:
                # This is a placeholder; pyrebase doesn't directly support resetting password with a new password
                # You would typically use the oobCode from the email link
                flash("Password reset functionality requires oobCode from email link. Please use the link sent to your email.", "warning")
                return redirect(url_for('login'))
            except Exception as e:
                flash(f"Error resetting password: {str(e)}", "danger")
        else:
            flash("Please login or use the reset link from your email.", "warning")
            return redirect(url_for('login'))
    return render_template('reset.html')

if __name__ == "__main__":
    app.run(debug=True)