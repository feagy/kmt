import os
import sys
import csv
import random
from dataclasses import dataclass, field

SUMO_HOME = r"C:\Program Files (x86)\Eclipse\Sumo"
PROJECT_DIR = r"C:\Projects\KMT"

sys.path.append(os.path.join(SUMO_HOME, "tools"))

import traci
import sumolib


SUMO_BINARY = os.path.join(SUMO_HOME, "bin", "sumo.exe")
SUMO_CONFIG = os.path.join(PROJECT_DIR, "simulation.sumocfg")
NET_FILE = os.path.join(PROJECT_DIR, "map.net.xml")


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

    @property
    def soc(self):
        return self.current_energy / self.battery_capacity * 100

    @property
    def q_min(self):
        return self.battery_capacity * self.min_soc_percent / 100

    @property
    def q_target(self):
        return self.battery_capacity * self.target_soc_percent / 100

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
        self.sessions = [
            session for session in self.sessions
            if session.finish_time > arrival_time
        ]

        if not self.sessions:
            return arrival_time

        latest_finish = max(session.finish_time for session in self.sessions)
        return max(arrival_time, latest_finish)

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

    def estimate_best_option(
        self,
        vehicle,
        current_time,
        keep_safety_margin=True
    ):
        route_info = self.get_route_info(vehicle.current_edge)

        if route_info is None:
            return None

        distance_km = route_info.distance_km

        if not vehicle.can_reach_station(distance_km, keep_safety_margin):
            return None

        travel_time = route_info.travel_time_min
        arrival_time = current_time + travel_time
        required_energy = vehicle.get_required_energy_at_station(distance_km)

        best_option = None

        for charger in self.chargers:
            available_time = charger.get_available_time(arrival_time)
            waiting_time = max(0.0, available_time - arrival_time)
            charging_time = charger.estimate_charging_time(required_energy)
            total_time = travel_time + waiting_time + charging_time

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
                "soc_at_arrival": vehicle.expected_soc_after_reaching_station(distance_km)
            }

            if best_option is None or total_time < best_option["total_time"]:
                best_option = option

        return best_option

    def reserve_charger(self, option, vehicle_id):
        charger = next(
            c for c in self.chargers
            if c.charger_id == option["charger_id"]
        )

        return charger.reserve(
            vehicle_id=vehicle_id,
            arrival_time=option["arrival_time"],
            required_energy=option["required_energy"]
        )


def select_best_station(vehicle, stations, current_time):
    options = []

    for station in stations:
        option = station.estimate_best_option(
            vehicle=vehicle,
            current_time=current_time
        )

        if option is not None:
            options.append(option)

    if not options:
        return None

    return min(options, key=lambda x: x["total_time"])


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


def main():
    os.chdir(PROJECT_DIR)

    #stations = create_random_stations(count=5)
    stations = [
        ChargingStation(
            station_id="CS_1",
            edge_id="97388510#1",
            chargers=[Charger("CS_1_CH_1", power_kw=50, efficiency=0.90)]
        ),
        ChargingStation(
            station_id="CS_2",
            edge_id="-97226043#0",
            chargers=[Charger("CS_2_CH_1", power_kw=100, efficiency=0.90)]
        ),
        ChargingStation(
            station_id="CS_3",
            edge_id="174851559#1",
            chargers=[Charger("CS_3_CH_1", power_kw=150, efficiency=0.90)]
        ),
    ]

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
    assigned_vehicles = set()
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

            vehicle = vehicles[vehicle_id]
            vehicle.current_edge = current_edge

            speed_mps = traci.vehicle.getSpeed(vehicle_id)
            distance_this_step_km = speed_mps / 1000
            vehicle.consume_energy(distance_this_step_km)

            if vehicle.soc <= 30 and vehicle_id not in assigned_vehicles:
                best = select_best_station(
                    vehicle=vehicle,
                    stations=stations,
                    current_time=current_time
                )

                if best is None:
                    records.append({
                        "time": current_time,
                        "vehicle_id": vehicle_id,
                        "event": "no_reachable_station",
                        "soc": round(vehicle.soc, 2)
                    })
                    continue

                station = next(
                    s for s in stations
                    if s.station_id == best["station_id"]
                )

                session = station.reserve_charger(
                    option=best,
                    vehicle_id=vehicle_id
                )

                try:
                    traci.vehicle.setRoute(vehicle_id, best["route_edges"])
                except traci.TraCIException:
                    continue

                assigned_vehicles.add(vehicle_id)

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
                    "charge_finish_time": round(session.finish_time, 2)
                }

                records.append(record)

                print(
                    f"[{current_time:.0f}s] {vehicle_id} -> "
                    f"{best['station_id']} | "
                    f"SoC={vehicle.soc:.1f}% | "
                    f"Total={best['total_time']:.2f} dk"
                )

    traci.close()

    if records:
        output_file = os.path.join(PROJECT_DIR, "cs_selection_results.csv")

        keys = sorted(set().union(*(r.keys() for r in records)))

        with open(output_file, "w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=keys)
            writer.writeheader()
            writer.writerows(records)

        print("CSV saved:", output_file)
    else:
        print("No records generated.")


if __name__ == "__main__":
    main()
