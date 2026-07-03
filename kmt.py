import os
import sys
import csv
import random
from dataclasses import dataclass, field
from pandas import options
import pyproj
import json                 

SUMO_HOME = r"C:\Program Files (x86)\Eclipse\Sumo"
PROJECT_DIR = r"C:\Projects\KMT"

sys.path.append(os.path.join(SUMO_HOME, "tools"))

import traci
import sumolib


SUMO_BINARY = os.path.join(SUMO_HOME, "bin", "sumo.exe")
SUMO_CONFIG = os.path.join(PROJECT_DIR, "simulation.sumocfg")
NET_FILE = os.path.join(PROJECT_DIR, "map.net.xml")
NET = sumolib.net.readNet(NET_FILE)

@dataclass
class Vehicle:
    vehicle_id: str
    battery_capacity: float
    current_energy: float
    consumption_per_km: float
    current_edge: str
    required_energy: float = 0.0
    min_soc_percent: float = 20.0
    target_soc_percent: float = 80.0

    max_charge_power_kw: float = 150.0
    charging_protocol: str = "CP-CV"
    u_low_v: float = 320.0
    u_high_v: float = 400.0
    taper_start_soc_percent: float = 80.0
    terminal_soc_percent: float = 99.0

    @property
    def soc(self):
        return self.current_energy / self.battery_capacity * 100

    @property
    def q_min(self):
        return self.battery_capacity * self.min_soc_percent / 100

    @property
    def q_target(self):
        effective_target_soc = min(
            self.target_soc_percent,
            self.terminal_soc_percent
        )

        return self.battery_capacity * effective_target_soc / 100

    def consume_energy(self, distance_km):
        self.current_energy -= distance_km * self.consumption_per_km
        self.current_energy = max(0.0, self.current_energy)

    def e_pred(self, distance_km):
        return distance_km * self.consumption_per_km

    def expected_energy_after_reaching_station(self, distance_km):
        return self.current_energy - self.e_pred(distance_km)

    def expected_soc_after_reaching_station(self, distance_km):
        return (
            self.expected_energy_after_reaching_station(distance_km)
            / self.battery_capacity
        ) * 100

    def can_reach_station(self, distance_km, keep_safety_margin=True):
        remaining = self.expected_energy_after_reaching_station(distance_km)

        if keep_safety_margin:
            return remaining >= self.q_min

        return remaining >= 0

    def get_required_energy_at_station(self, distance_km):
        if self.required_energy > 0:
            return self.required_energy

        energy_after_arrival = self.expected_energy_after_reaching_station(distance_km)
        return max(0.0, self.q_target - energy_after_arrival)


@dataclass
class QueueEntry:
    vehicle_id: str
    start_time: float
    charging_duration: float

    @property
    def finish_time(self):
        return self.start_time + self.charging_duration


@dataclass
class Charger:
    charger_id: str
    power_kw: float
    efficiency: float = 0.90
    sessions: list = field(default_factory=list)

    def get_available_time(self, arrival_time):
        self.clean_finished_sessions(arrival_time)

        self.sessions = [
            session for session in self.sessions
            if session.finish_time > arrival_time
        ]

        if not self.sessions:
            return arrival_time

        latest_finish = max(session.finish_time for session in self.sessions)
        return max(arrival_time, latest_finish)

    def get_effective_max_power_kw(self, vehicle):
        return min(
            self.power_kw,
            vehicle.max_charge_power_kw
        )
    
    def get_instantaneous_power_kw(self, vehicle, soc_percent):
        soc = max(0.0, min(soc_percent, 100.0)) / 100

        taper_soc = vehicle.taper_start_soc_percent / 100
        p_max_kw = self.get_effective_max_power_kw(vehicle)

        u_low = vehicle.u_low_v
        u_high = vehicle.u_high_v

        if p_max_kw <= 0 or u_high <= 0:
            return 0.0

        i_max_a = (p_max_kw * 1000) / u_high

        if soc < taper_soc:
            current_a = i_max_a
        else:
            taper_ratio = (1 - soc) / (1 - taper_soc)
            current_a = max(0.0, taper_ratio * i_max_a)

        if soc < taper_soc:
            voltage_v = u_low + (
                (soc / taper_soc) * (u_high - u_low)
            )
        else:
            voltage_v = u_high

        protocol = vehicle.charging_protocol.upper()

        if protocol == "CC-CV":
            power_kw = (voltage_v * current_a) / 1000

        elif protocol == "CP-CV":
            if soc < taper_soc:
                power_kw = p_max_kw
            else:
                power_kw = (voltage_v * current_a) / 1000

        else:
            raise ValueError(
                "charging_protocol yalnızca 'CC-CV' veya 'CP-CV' olabilir."
            )

        return min(power_kw, p_max_kw)

    def estimate_charging_time(
        self,
        vehicle,
        soc_at_arrival_percent,
        step_minutes=0.25
    ):
        """
        Denklem 1.23'e göre zaman adımlı enerji aktarımı yapar.
        0.25 dk = 15 saniyelik adım kullanılır.
        """
        start_soc = max(0.0, soc_at_arrival_percent)

        target_soc = min(
            vehicle.target_soc_percent,
            vehicle.terminal_soc_percent
        )

        if start_soc >= target_soc:
            return 0.0

        current_energy = (
            vehicle.battery_capacity * start_soc / 100
        )

        target_energy = (
            vehicle.battery_capacity * target_soc / 100
        )

        elapsed_minutes = 0.0

        while current_energy < target_energy:
            current_soc = (
                current_energy / vehicle.battery_capacity
            ) * 100

            power_kw = self.get_instantaneous_power_kw(
                vehicle,
                current_soc
            )

            battery_power_kw = power_kw * self.efficiency

            if battery_power_kw <= 0:
                raise ValueError(
                    "Şarj gücü sıfırlandı; hedef SoC hesaplanamıyor."
                )

            energy_this_step = (
                battery_power_kw * step_minutes / 60
            )

            remaining_energy = target_energy - current_energy

            if energy_this_step >= remaining_energy:
                elapsed_minutes += (
                    remaining_energy / battery_power_kw
                ) * 60
                break

            current_energy += energy_this_step
            elapsed_minutes += step_minutes

        return elapsed_minutes

    def reserve(
        self,
        vehicle_id,
        arrival_time,
        charging_duration
    ):
        available_time = self.get_available_time(arrival_time)

        start_time = max(arrival_time, available_time)

        entry = QueueEntry(
            vehicle_id=vehicle_id,
            start_time=start_time,
            charging_duration=charging_duration
        )

        self.sessions.append(entry)
        return entry

    """
    def estimate_charging_time(self, required_energy):
        if required_energy <= 0:
            return 0.0

        return required_energy / (self.power_kw * self.efficiency) * 60

    def reserve(self, vehicle_id, arrival_time, required_energy):
        available_time = self.get_available_time(arrival_time)
        start_time = max(arrival_time, available_time)
        charging_duration = self.estimate_charging_time(required_energy)

        entry = QueueEntry(
            vehicle_id=vehicle_id,
            start_time=start_time,
            charging_duration=charging_duration
        )

        self.sessions.append(entry)
        return entry
    """
    def clean_finished_sessions(self, current_time):
        self.sessions = [
            session for session in self.sessions
            if session.finish_time > current_time
        ]

@dataclass
class RouteInfo:
    distance_km: float
    travel_time_min: float
    edges: tuple


@dataclass
class ChargingStation:
    station_id: str
    edge_id: str
    chargers: list

    def get_route_info(self, vehicle_edge):
        if vehicle_edge.startswith(":"):
            return None

        try:
            route = traci.simulation.findRoute(vehicle_edge, self.edge_id)
        except traci.TraCIException:
            return None

        if not route.edges:
            return None

        return RouteInfo(
            distance_km=route.length / 1000,
            travel_time_min=route.travelTime / 60,
            edges=route.edges
        )

    def estimate_best_option_from_route(
        self,
        vehicle,
        route_info,
        current_time,
        keep_safety_margin=True
    ):
        if route_info is None:
            return None

        distance_km = route_info.distance_km

        if not vehicle.can_reach_station(
            distance_km,
            keep_safety_margin
        ):
            return None

        travel_time = route_info.travel_time_min
        arrival_time = current_time + travel_time

        required_energy = vehicle.get_required_energy_at_station(
            distance_km
        )

        best_option = None

        for charger in self.chargers:
            available_time = charger.get_available_time(arrival_time)

            waiting_time = max(
                0.0,
                available_time - arrival_time
            )

            soc_at_arrival = vehicle.expected_soc_after_reaching_station(
                distance_km
            )

            charging_time = charger.estimate_charging_time(
                vehicle=vehicle,
                soc_at_arrival_percent=soc_at_arrival
            )

            total_time = (
                travel_time
                + waiting_time
                + charging_time
            )

            option = {
                "station_id": self.station_id,
                "charger_id": charger.charger_id,
                "station_edge": self.edge_id,
                "route_edges": route_info.edges,
                "distance_km": distance_km,
                "travel_time": travel_time,
                "arrival_time": arrival_time,
                "waiting_time": waiting_time,
                "charging_time": charging_time,
                "total_time": total_time,
                "required_energy": required_energy,
                "soc_now": vehicle.soc,
                "soc_at_arrival": soc_at_arrival,
                "queue_length": len(charger.sessions),
                "charger_power_kw": charger.power_kw,
            }

            if (
                best_option is None
                or option["total_time"] < best_option["total_time"]
            ):
                best_option = option

        return best_option

    def estimate_best_option(
        self,
        vehicle,
        current_time,
        keep_safety_margin=True
    ):
        route_info = self.get_route_info(vehicle.current_edge)

        return self.estimate_best_option_from_route(
            vehicle=vehicle,
            route_info=route_info,
            current_time=current_time,
            keep_safety_margin=keep_safety_margin
        )

    def reserve_charger(self, option, vehicle_id):
        charger = next(
            c for c in self.chargers
            if c.charger_id == option["charger_id"]
        )

        return charger.reserve(
            vehicle_id=vehicle_id,
            arrival_time=option["arrival_time"],
            charging_duration=option["charging_time"]
        )
    
    def get_station_position(self):
        edge = NET.getEdge(self.edge_id)

        shape = edge.getShape()

        x, y = shape[len(shape) // 2]

        return x, y

def get_reachable_station_options(vehicle, stations, current_time):
    options = []

    for station in stations:
        option = station.estimate_best_option(
            vehicle=vehicle,
            current_time=current_time
        )

        if option is not None:
            options.append(option)

    return options

def create_random_stations(count=5):
    net = sumolib.net.readNet(NET_FILE)

    candidate_edges = [
        edge.getID()
        for edge in net.getEdges()
        if edge.allows("passenger") and not edge.getID().startswith(":")
    ]

    selected_edges = random.sample(candidate_edges, count)

    powers = [22, 50, 100, 150, 250]

    stations = []

    for i, edge_id in enumerate(selected_edges, start=1):
        station = ChargingStation(
            station_id=f"CS_{i}",
            edge_id=edge_id,
            chargers=[
                Charger(
                    charger_id=f"CS_{i}_CH_1",
                    power_kw=random.choice(powers),
                    efficiency=random.uniform(0.85, 0.95)
                )
            ]
        )

        stations.append(station)

    return stations

def route_edges_to_latlon(route_edges):
    coords = []

    for edge_id in route_edges:
        if edge_id.startswith(":"):
            continue

        try:
            edge = NET.getEdge(edge_id)
        except KeyError:
            continue

        for x, y in edge.getShape():
            lon, lat = NET.convertXY2LonLat(x, y)

            point = [round(lat, 6), round(lon, 6)]

            if not coords or coords[-1] != point:
                coords.append(point)

    return coords

def create_default_stations(seed_demo_queue=False):
    stations = [
        ChargingStation(
            station_id="CS_1",
            edge_id="97388510#1",
            chargers=[
                Charger(
                    "CS_1_CH_1",
                    power_kw=50,
                    efficiency=0.90
                )
            ]
        ),
        ChargingStation(
            station_id="CS_2",
            edge_id="-97226043#0",
            chargers=[
                Charger(
                    "CS_2_CH_1",
                    power_kw=100,
                    efficiency=0.90
                )
            ]
        ),
        ChargingStation(
            station_id="CS_3",
            edge_id="174851559#1",
            chargers=[
                Charger(
                    "CS_3_CH_1",
                    power_kw=150,
                    efficiency=0.90
                )
            ]
        ),
    ]

    if seed_demo_queue:
        stations[0].chargers[0].sessions.append(
            QueueEntry(
                vehicle_id="demo_vehicle_1",
                start_time=0,
                charging_duration=20
            )
        )

        stations[1].chargers[0].sessions.append(
            QueueEntry(
                vehicle_id="demo_vehicle_2",
                start_time=0,
                charging_duration=10
            )
        )

    return stations


def analyze_interactive_location(
    lat,
    lon,
    battery_capacity,
    current_soc_percent,
    consumption_per_km,
    target_soc_percent,
    stations,
    current_time=0.0,
    min_soc_percent=20.0
):

    source_edge = find_nearest_passenger_edge(lon, lat)

    if source_edge is None:
        raise ValueError(
            "Seçilen konum SUMO yol ağı üzerindeki bir araç yoluna bağlanamadı."
        )

    current_energy = (
        battery_capacity
        * current_soc_percent
        / 100
    )

    vehicle = Vehicle(
        vehicle_id="interactive_user",
        battery_capacity=battery_capacity,
        current_energy=current_energy,
        consumption_per_km=consumption_per_km,
        current_edge=source_edge.getID(),
        min_soc_percent=min_soc_percent,
        target_soc_percent=target_soc_percent
    )

    options = []

    for station in stations:
        route_info = get_static_route_info(
            source_edge.getID(),
            station.edge_id
        )

        option = station.estimate_best_option_from_route(
            vehicle=vehicle,
            route_info=route_info,
            current_time=current_time
        )

        if option is None:
            continue

        station_x, station_y = station.get_station_position()

        station_lon, station_lat = NET.convertXY2LonLat(
            station_x,
            station_y
        )

        route_coords = [
            [round(lat, 6), round(lon, 6)]
        ]

        route_coords.extend(
            route_edges_to_latlon(option["route_edges"])
        )

        route_coords.append(
            [round(station_lat, 6), round(station_lon, 6)]
        )

        option.update({
            "vehicle_lat": round(lat, 6),
            "vehicle_lon": round(lon, 6),
            "station_lat": round(station_lat, 6),
            "station_lon": round(station_lon, 6),
            "route_coords": route_coords,
            "source_edge": source_edge.getID(),
        })

        options.append(option)

    if not options:
        raise ValueError(
            "Seçilen konumdan menzil içinde ve SUMO yol ağı üzerinden "
            "erişilebilen istasyon bulunamadı. "
            "Haritada simülasyon ağının kapsadığı bölge içinde bir nokta seçin."
        )

    best_total = min(
        options,
        key=lambda item: item["total_time"]
    )

    best_distance = min(
        options,
        key=lambda item: item["distance_km"]
    )

    best_waiting = min(
        options,
        key=lambda item: item["waiting_time"]
    )

    best_charging = min(
        options,
        key=lambda item: item["charging_time"]
    )

    for option in options:
        option["is_best_total_time"] = (
            option["charger_id"] == best_total["charger_id"]
        )

        option["is_best_distance"] = (
            option["charger_id"] == best_distance["charger_id"]
        )

        option["is_best_waiting"] = (
            option["charger_id"] == best_waiting["charger_id"]
        )

        option["is_best_charging"] = (
            option["charger_id"] == best_charging["charger_id"]
        )

    return sorted(
        options,
        key=lambda item: item["total_time"]
    )

def main():
    try:
        os.chdir(PROJECT_DIR)

        #stations = create_random_stations(count=5)
        stations = create_default_stations()

        print("Charging stations:")
        for station in stations:
            charger = station.chargers[0]
            print(
                station.station_id,
                station.edge_id,
                charger.power_kw,
                round(charger.efficiency, 2)
            )

        traci.start([
            SUMO_BINARY,
            "-c", SUMO_CONFIG,
            "--no-step-log", "true"
        ])

        vehicles = {}
        vehicle_states = {}
        records = []

        while traci.simulation.getMinExpectedNumber() > 0:
            traci.simulationStep()

            current_time = traci.simulation.getTime() / 60

            for vehicle_id in traci.vehicle.getIDList():
                current_edge = traci.vehicle.getRoadID(vehicle_id)

                if current_edge.startswith(":"):
                    continue

                if vehicle_id not in vehicles:
                    vehicles[vehicle_id] = Vehicle(
                        vehicle_id=vehicle_id,
                        battery_capacity=35.0,
                        current_energy=random.uniform(10.0, 22.0),
                        consumption_per_km=0.5,
                        current_edge=current_edge,
                        required_energy=0.0,
                        target_soc_percent=80.0
                    )

                    vehicle_states[vehicle_id] = {
                        "status": "driving",
                        "selected_station_id": None,
                        "selected_charger_id": None,
                        "charge_start_time": None,
                        "charge_finish_time": None,
                        "required_energy": None
                    }

                vehicle = vehicles[vehicle_id]
                vehicle.current_edge = current_edge

                state = vehicle_states[vehicle_id]

                if state["status"] == "charging":
                    if current_time >= state["charge_finish_time"]:

                        vehicle.current_energy += state["required_energy"]
                        vehicle.current_energy = min(
                            vehicle.current_energy,
                            vehicle.battery_capacity
                        )

                        state["status"] = "done"

                        records.append({
                            "time": round(current_time, 2),
                            "vehicle_id": vehicle_id,
                            "event": "charging_finished",
                            "station_id": state["selected_station_id"],
                            "charger_id": state["selected_charger_id"],
                            "final_soc": round(vehicle.soc, 2),
                            "current_energy": round(vehicle.current_energy, 2)
                        })

                        print(
                            f"[{current_time:.2f} min] {vehicle_id} finished charging | "
                            f"SoC={vehicle.soc:.2f}%"
                        )

                    continue

                if state["status"] == "going_to_charge":
                    selected_station = next(
                        station for station in stations
                        if station.station_id == state["selected_station_id"]
                    )

                    if current_edge == selected_station.edge_id:
                        state["status"] = "charging"

                        records.append({
                            "time": round(current_time, 2),
                            "vehicle_id": vehicle_id,
                            "event": "arrived_at_station",
                            "station_id": selected_station.station_id,
                            "edge_id": current_edge,
                            "soc": round(vehicle.soc, 2)
                        })

                        print(
                            f"[{current_time:.2f} min] {vehicle_id} arrived at "
                            f"{selected_station.station_id}"
                        )

                if state["status"] in ["going_to_charge", "driving"]:
                    speed_mps = traci.vehicle.getSpeed(vehicle_id)
                    distance_this_step_km = speed_mps / 1000
                    vehicle.consume_energy(distance_this_step_km)
                
                

                if vehicle.soc <= 30 and state["status"] == "driving":
                    options = get_reachable_station_options(
                        vehicle=vehicle,
                        stations=stations,
                        current_time=current_time
                    )

                    if not options:
                        records.append({
                            "time": round(current_time, 2),
                            "vehicle_id": vehicle_id,
                            "event": "no_reachable_station",
                            "soc": round(vehicle.soc, 2)
                        })
                        continue
                    
                    best_total = min(options, key=lambda x: x["total_time"])
                    best_distance = min(options, key=lambda x: x["distance_km"])
                    best_waiting = min(options, key=lambda x: x["waiting_time"])
                    best_charging = min(options, key=lambda x: x["charging_time"])

                    x, y = traci.vehicle.getPosition(vehicle_id)
                    vehicle_lon, vehicle_lat = NET.convertXY2LonLat(x, y)


                    for option in options:
                        station = next(
                            s for s in stations
                            if s.station_id == option["station_id"]
                        )

                        station_x, station_y = station.get_station_position()
                        station_lon, station_lat = NET.convertXY2LonLat(station_x, station_y)

                        route_coords = [
                            [round(vehicle_lat, 6), round(vehicle_lon, 6)]
                        ]

                        route_coords.extend(
                            route_edges_to_latlon(option["route_edges"])
                        )

                        route_coords.append(
                            [round(station_lat, 6), round(station_lon, 6)]
                        )

                        records.append({
                            "time": round(current_time, 2),
                            "vehicle_id": vehicle_id,
                            "event": "station_option_calculated",
                            "station_id": option["station_id"],
                            "charger_id": option["charger_id"],
                            "soc_now": round(option["soc_now"], 2),
                            "soc_at_arrival": round(option["soc_at_arrival"], 2),
                            "distance_km": round(option["distance_km"], 3),
                            "travel_time_min": round(option["travel_time"], 2),
                            "waiting_time_min": round(option["waiting_time"], 2),
                            "charging_time_min": round(option["charging_time"], 2),
                            "total_time_min": round(option["total_time"], 2),
                            "required_energy": round(option["required_energy"], 2),
                            "is_best_total_time": option["station_id"] == best_total["station_id"],
                            "is_best_distance": option["station_id"] == best_distance["station_id"],
                            "is_best_waiting": option["station_id"] == best_waiting["station_id"],
                            "is_best_charging": option["station_id"] == best_charging["station_id"],
                            "vehicle_lat": round(vehicle_lat, 6),
                            "vehicle_lon": round(vehicle_lon, 6),
                            "station_lat": round(station_lat, 6),
                            "station_lon": round(station_lon, 6),
                            "route_coords": json.dumps(route_coords),
                            "route_edges": json.dumps(list(option["route_edges"])),
                        })
                    
                    best = best_total

                    station = next(
                        s for s in stations
                        if s.station_id == best["station_id"]
                    )

                    session = station.reserve_charger(
                        option=best,
                        vehicle_id=vehicle_id
                    )

                    station_x, station_y = station.get_station_position()

                    try:
                        traci.vehicle.setRoute(vehicle_id, best["route_edges"])
                    except traci.TraCIException:
                        continue

                    state["status"] = "going_to_charge"
                    state["selected_station_id"] = best["station_id"]
                    state["selected_charger_id"] = best["charger_id"]
                    state["charge_start_time"] = session.start_time
                    state["charge_finish_time"] = session.finish_time
                    state["required_energy"] = best["required_energy"]

                    selected_charger = next(
                        charger for charger in station.chargers
                        if charger.charger_id == best["charger_id"]
                    )

                    x, y = traci.vehicle.getPosition(vehicle_id)
                    vehicle_lon, vehicle_lat = NET.convertXY2LonLat(x, y)
                    station_lon, station_lat = NET.convertXY2LonLat(station_x, station_y)

                    record = {
                        "time": round(current_time, 2),
                        "vehicle_id": vehicle_id,
                        "event": "station_selected",
                        "station_id": best["station_id"],
                        "charger_id": best["charger_id"],
                        "station_edge": best["station_edge"],
                        "soc_now": round(best["soc_now"], 2),
                        "soc_at_arrival": round(best["soc_at_arrival"], 2),
                        "distance_km": round(best["distance_km"], 3),
                        "travel_time_min": round(best["travel_time"], 2),
                        "waiting_time_min": round(best["waiting_time"], 2),
                        "charging_time_min": round(best["charging_time"], 2),
                        "total_time_min": round(best["total_time"], 2),
                        "required_energy": round(best["required_energy"], 2),
                        "charge_start_time": round(session.start_time, 2),
                        "charge_finish_time": round(session.finish_time, 2),
                        "status": state["status"],
                        "queue_length": len(selected_charger.sessions),
                        "current_energy": round(vehicle.current_energy, 2),
                        "battery_capacity": vehicle.battery_capacity,
                        "target_soc": vehicle.target_soc_percent,
                        "queue_length_at_selection": len(selected_charger.sessions),
                        "current_energy": round(vehicle.current_energy, 2),
                        "battery_capacity": vehicle.battery_capacity,
                        "vehicle_x": round(x, 2),
                        "vehicle_y": round(y, 2),
                        "station_x": round(station_x, 2),
                        "station_y": round(station_y, 2),
                        "vehicle_lon": round(vehicle_lon, 6),
                        "vehicle_lat": round(vehicle_lat, 6),
                        "station_lon": round(station_lon, 6),
                        "station_lat": round(station_lat, 6),
                    }

                    records.append(record)

                    print(
                        f"[{current_time:.0f}s] {vehicle_id} -> "
                        f"{best['station_id']} | "
                        f"SoC={vehicle.soc:.1f}% | "
                        f"Total={best['total_time']:.2f} dk"
                    )
    finally:
        traci.close()
        save_records(records)




def save_records(records):
    if records:
        print(f"Saving {len(records)} records to CSV...")
        output_file = os.path.join(PROJECT_DIR, "cs_selection_results.csv")
        keys = sorted(set().union(*(r.keys() for r in records)))

        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(records)

        print("CSV saved:", output_file)
    else:
        print("No records generated.")


def find_nearest_passenger_edge(lon, lat):
    x, y = NET.convertLonLat2XY(lon, lat)

    for radius in [25, 50, 100, 250, 500, 1000]:
        candidates = [
            (edge, distance)
            for edge, distance in NET.getNeighboringEdges(x, y, radius)
            if not edge.getID().startswith(":") and edge.allows("passenger")
        ]

        if candidates:
            closest_edge, _ = min(candidates, key=lambda item: item[1])
            return closest_edge

    return None

def get_static_route_info(from_edge_id, to_edge_id):
    try:
        from_edge = NET.getEdge(from_edge_id)
        to_edge = NET.getEdge(to_edge_id)
    except KeyError:
        return None

    route_edges, travel_time_sec = NET.getFastestPath(
        from_edge,
        to_edge,
        vClass="passenger"
    )

    if not route_edges:
        return None

    distance_km = sum(
        edge.getLength()
        for edge in route_edges
    ) / 1000

    return RouteInfo(
        distance_km=distance_km,
        travel_time_min=travel_time_sec / 60,
        edges=tuple(edge.getID() for edge in route_edges)
    )

def route_edges_to_latlon(route_edges):
    coords = []

    for edge_id in route_edges:
        if edge_id.startswith(":"):
            continue

        try:
            edge = NET.getEdge(edge_id)
        except KeyError:
            continue

        for x, y in edge.getShape():
            lon, lat = NET.convertXY2LonLat(x, y)

            point = [round(lat, 6), round(lon, 6)]

            if not coords or coords[-1] != point:
                coords.append(point)

    return coords

if __name__ == "__main__":
    main()
