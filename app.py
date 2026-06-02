import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium

st.set_page_config(page_title="EV Charging Station Selection", layout="wide")

st.title("EV Charging Station Selection Prototype")

CSV_PATH = "cs_selection_results.csv"

df = pd.read_csv(CSV_PATH)

left, center, right = st.columns([1, 2, 1])

with left:
    st.header("Vehicles")

    vehicle_ids = df["vehicle_id"].unique()
    selected_vehicle = st.selectbox("Select vehicle", vehicle_ids)

    vehicle_df = df[df["vehicle_id"] == selected_vehicle]

    st.dataframe(
        vehicle_df[
            [
                "time",
                "event",
                "soc_now",
                "soc_at_arrival",
                "station_id",
                "total_time_min",
            ]
        ],
        use_container_width=True
    )

with center:
    st.header("Map / Scenario View")

    vehicle_row = vehicle_df.iloc[-1]

    if "vehicle_lat" in vehicle_row and pd.notna(vehicle_row["vehicle_lat"]):
        map_center = [vehicle_row["vehicle_lat"], vehicle_row["vehicle_lon"]]
    else:
        map_center = [41.0, 28.7]

    m = folium.Map(location=map_center, zoom_start=14)

    folium.Marker(
        [
            vehicle_row["vehicle_lat"],
            vehicle_row["vehicle_lon"]
        ],
        popup=f"Vehicle {selected_vehicle}"
    ).add_to(m)

    folium.Marker(
        [
            vehicle_row["station_lat"],
            vehicle_row["station_lon"]
        ],
        popup=vehicle_row["station_id"]
    ).add_to(m)

    st_folium(m, width=900, height=600)

with right:
    st.header("Summary")

    selected_events = df[df["event"] == "station_selected"]

    st.metric("Selected Vehicles", len(selected_events))

    st.metric(
        "Avg Total Time",
        f"{selected_events['total_time_min'].mean():.2f} min"
    )

    st.metric(
        "Avg Waiting Time",
        f"{selected_events['waiting_time_min'].mean():.2f} min"
    )

    st.metric(
        "Avg Charging Time",
        f"{selected_events['charging_time_min'].mean():.2f} min"
    )

    st.subheader("Station Usage")
    st.bar_chart(selected_events["station_id"].value_counts())