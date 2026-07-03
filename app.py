import pandas as pd
import streamlit as st
import folium
from streamlit_folium import st_folium
import json
from kmt import (
    analyze_interactive_location,
    create_default_stations
)
import altair as alt

st.set_page_config(page_title="Elektrikli Araç Şarj İstasyonu Seçimi", layout="wide")

st.title("Elektrikli Araç Şarj İstasyonu Seçimi")


if "interactive_stations" not in st.session_state:
    st.session_state["interactive_stations"] = (
        create_default_stations()
    )

if "live_options" not in st.session_state:
    st.session_state["live_options"] = []

if "map_version" not in st.session_state:
    st.session_state["map_version"] = 0

CSV_PATH = "cs_selection_results.csv"

df = pd.read_csv(CSV_PATH)

options_df = df[df["event"] == "station_option_calculated"]
selected_df = df[df["event"] == "station_selected"]

CSV_STRATEGY_COLUMNS = {
    "En kısa toplam süre": "total_time_min",
    "En kısa mesafe": "distance_km",
    "En kısa bekleme": "waiting_time_min",
    "En kısa şarj süresi": "charging_time_min",
}

def get_strategy_choices(options, strategy_column):
    decision_columns = ["vehicle_id", "time"]

    valid_options = options.dropna(
        subset=decision_columns + [strategy_column]
    ).copy()

    selected_indexes = (
        valid_options
        .groupby(decision_columns)[strategy_column]
        .idxmin()
    )

    return valid_options.loc[selected_indexes].copy()

def build_strategy_summary(options):
    summary_rows = []

    for strategy_name, strategy_column in CSV_STRATEGY_COLUMNS.items():
        selected_by_strategy = get_strategy_choices(
            options,
            strategy_column
        )

        if selected_by_strategy.empty:
            continue

        most_used_station = (
            selected_by_strategy["station_id"]
            .value_counts()
            .idxmax()
        )

        summary_rows.append({
            "Strateji": strategy_name,
            "Karar Sayısı": len(selected_by_strategy),
            "Ort. Mesafe (km)": selected_by_strategy["distance_km"].mean(),
            "Ort. Yolculuk (dk)": selected_by_strategy["travel_time_min"].mean(),
            "Ort. Bekleme (dk)": selected_by_strategy["waiting_time_min"].mean(),
            "Ort. Şarj (dk)": selected_by_strategy["charging_time_min"].mean(),
            "Ort. Toplam Süre (dk)": selected_by_strategy["total_time_min"].mean(),
            "Beklemesiz Seçim (%)": (
                selected_by_strategy["waiting_time_min"] <= 0.01
            ).mean() * 100,
            "En Çok Seçilen İstasyon": most_used_station,
        })

    return pd.DataFrame(summary_rows)

def show_strategy_metrics(row):
    with st.container(border=True):
        st.subheader(row["Strateji"])

        st.caption(
            "Bu değerler, her karar anında bu stratejinin seçmiş "
            "olacağı istasyonların ortalamasıdır."
        )

        main_1, main_2, main_3, main_4 = st.columns(4)

        main_1.metric(
            "Ort. Mesafe",
            f'{row["Ort. Mesafe (km)"]:.2f} km'
        )

        main_2.metric(
            "Ort. Bekleme",
            f'{row["Ort. Bekleme (dk)"]:.2f} dk'
        )

        main_3.metric(
            "Ort. Şarj Süresi",
            f'{row["Ort. Şarj (dk)"]:.2f} dk'
        )

        main_4.metric(
            "Ort. Toplam Süre",
            f'{row["Ort. Toplam Süre (dk)"]:.2f} dk'
        )

        detail_1, detail_2 = st.columns(2)

        detail_1.metric(
            "Beklemesiz Seçim",
            f'%{row["Beklemesiz Seçim (%)"]:.1f}'
        )

        detail_2.metric(
            "En Çok Seçilen İstasyon",
            row["En Çok Seçilen İstasyon"]
        )

EV_PROFILES = {
    "Compact EV (Demo)": {
        "battery_kwh": 52.0,
        "consumption_kwh_km": 0.17,
        "max_charge_kw": 50,
        "connector": "Type 2 / CCS",
        "reserve_soc": 20,
    },
    "Sedan EV (Demo)": {
        "battery_kwh": 60.0,
        "consumption_kwh_km": 0.15,
        "max_charge_kw": 170,
        "connector": "CCS",
        "reserve_soc": 20,
    },
    "SUV EV (Demo)": {
        "battery_kwh": 88.0,
        "consumption_kwh_km": 0.20,
        "max_charge_kw": 180,
        "connector": "CCS",
        "reserve_soc": 20,
    },
}

STRATEGY_MAP = {
    "En kısa toplam süre": "total_time",
    "En kısa mesafe": "distance_km",
    "En kısa bekleme": "waiting_time",
    "En kısa şarj süresi": "charging_time",
}

def build_analysis_map(user_location, options):
    lat = user_location["lat"]
    lon = user_location["lon"]

    m = folium.Map(
        location=[lat, lon],
        zoom_start=13,
        control_scale=True
    )

    folium.Marker(
        location=[lat, lon],
        tooltip="Seçilen araç konumu",
        popup="Analiz başlangıç noktası",
        icon=folium.Icon(
            color="red",
            icon="car",
            prefix="fa"
        )
    ).add_to(m)

    bounds = [[lat, lon]]

    for option in options:
        style = get_station_style(option)

        badges = []

        if option.get("is_best_total_time", False):
            badges.append("En kısa toplam süre")

        if option.get("is_best_distance", False):
            badges.append("En kısa mesafe")

        if option.get("is_best_waiting", False):
            badges.append("En kısa bekleme")

        if option.get("is_best_charging", False):
            badges.append("En kısa şarj")

        badge_text = "<br>".join(badges) or "Alternatif istasyon"

        popup_html = f"""
        <b>{option["station_id"]}</b><br>
        <b>{badge_text}</b><br><br>
        Şarj gücü: {option["charger_power_kw"]:.0f} kW<br>
        Mesafe: {option["distance_km"]:.2f} km<br>
        Yolculuk: {option["travel_time"]:.2f} dk<br>
        Bekleme: {option["waiting_time"]:.2f} dk<br>
        Şarj: {option["charging_time"]:.2f} dk<br>
        <b>Toplam: {option["total_time"]:.2f} dk</b><br>
        Varış SoC: %{option["soc_at_arrival"]:.1f}
        """

        station_layer = folium.FeatureGroup(
            name=f'{option["station_id"]} — {style["label"]}',
            show=True
        )

        route_coords = option.get("route_coords", [])

        if route_coords:
            folium.PolyLine(
                route_coords,
                color=style["color"],
                weight=5,
                opacity=0.75,
                tooltip=f'{option["station_id"]} rotası',
                popup=folium.Popup(popup_html, max_width=300)
            ).add_to(station_layer)

            bounds.extend(route_coords)

        folium.Marker(
            location=[
                option["station_lat"],
                option["station_lon"]
            ],
            tooltip=f'{option["station_id"]} — {style["label"]}',
            popup=folium.Popup(popup_html, max_width=300),
            icon=folium.Icon(
                color=style["color"],
                icon=style["icon"],
                prefix="fa"
            )
        ).add_to(station_layer)

        station_layer.add_to(m)

        bounds.append(
            [option["station_lat"], option["station_lon"]]
        )

    if options:
        folium.LayerControl(collapsed=True).add_to(m)

    if len(bounds) > 1:
        m.fit_bounds(bounds, padding=(20, 20))

    return m

def get_latest_options_for_vehicle(options_df, vehicle_id):
    vehicle_options = options_df[
        options_df["vehicle_id"] == vehicle_id
    ].copy()

    if vehicle_options.empty:
        return vehicle_options

    latest_time = vehicle_options["time"].max()

    return (
        vehicle_options[vehicle_options["time"] == latest_time]
        .sort_values("total_time_min")
        .drop_duplicates("station_id")
    )


vehicle_ids = sorted(df["vehicle_id"].dropna().unique().tolist())

if "results_vehicle" not in st.session_state:
    st.session_state["results_vehicle"] = vehicle_ids[0]

if "user_location" not in st.session_state:
    st.session_state["user_location"] = {
        "lat": 41.015,
        "lon": 28.979,
    }

if "analysis_strategy" not in st.session_state:
    st.session_state["analysis_strategy"] = "En kısa toplam süre"

def parse_route_coords(value):
    if pd.isna(value):
        return []

    try:
        return json.loads(value)
    except (json.JSONDecodeError, TypeError):
        return []

def get_station_style(option):
    if option["is_best_total_time"]:
        return {
            "color": "green",
            "icon": "star",
            "label": "En kısa toplam süre"
        }

    if option["is_best_distance"]:
        return {
            "color": "blue",
            "icon": "road",
            "label": "En kısa mesafe"
        }

    if option["is_best_waiting"]:
        return {
            "color": "orange",
            "icon": "clock-o",
            "label": "En kısa bekleme"
        }

    if option["is_best_charging"]:
        return {
            "color": "purple",
            "icon": "bolt",
            "label": "En kısa şarj"
        }

    return {
        "color": "gray",
        "icon": "plug",
        "label": "Diğer alternatif"
    }

tab_analysis, tab_results = st.tabs(
    [
        "Uygulama",
        "Simülasyon Sonuçları",
    ]
)

with tab_analysis:
    st.header("Araç Bilgileri ve Şarj Analizi")

    profile_col, map_col = st.columns([1, 1.4])

    with profile_col:
        selected_model = st.selectbox(
            "Araç tipi",
            list(EV_PROFILES.keys())
        )

        profile = EV_PROFILES[selected_model]

        current_soc = st.slider(
            "Mevcut batarya seviyesi (%)",
            min_value=5,
            max_value=100,
            value=45
        )

        target_soc = st.slider(
            "Hedef batarya seviyesi (%)",
            min_value=50,
            max_value=100,
            value=80
        )

        strategy_label = st.selectbox(
            "İstasyon seçim stratejisi",
            list(STRATEGY_MAP.keys()),
            key="analysis_strategy"
        )

        usable_energy = (
            profile["battery_kwh"]
            * max(current_soc - profile["reserve_soc"], 0)
            / 100
        )

        estimated_range_km = (
            usable_energy / profile["consumption_kwh_km"]
        )

        st.subheader("Araç Özellikleri")

        c1, c2 = st.columns(2)

        c1.metric(
            "Batarya Kapasitesi",
            f'{profile["battery_kwh"]:.0f} kWh'
        )

        c2.metric(
            "Tahmini Menzil",
            f"{estimated_range_km:.0f} km"
        )

        c1.metric(
            "Maks. Şarj Gücü",
            f'{profile["max_charge_kw"]} kW'
        )

        c2.metric(
            "Güvenlik SoC",
            f'%{profile["reserve_soc"]}'
        )

        st.caption(f'Soket tipi: {profile["connector"]}')
        st.caption(
            f"Seçilen hedef doluluk: %{target_soc}"
        )

    with map_col:
        st.subheader("Konum ve İstasyonlar")

        location = st.session_state["user_location"]

        current_signature = (
            selected_model,
            current_soc,
            target_soc,
            round(location["lat"], 6),
            round(location["lon"], 6),
        )

        if (
            st.session_state.get("analysis_signature") == current_signature
        ):
            options_to_show = st.session_state.get("live_options", [])
        else:
            options_to_show = []

        location_map = build_analysis_map(
            user_location=location,
            options=options_to_show
        )

        map_click = st_folium(
            location_map,
            width=700,
            height=430,
            key=f"location_picker_{st.session_state['map_version']}"
        )

        clicked_location = map_click.get("last_clicked")

        if clicked_location:
            new_location = {
                "lat": round(clicked_location["lat"], 6),
                "lon": round(clicked_location["lng"], 6),
            }

            old_location = st.session_state["user_location"]

            if new_location != old_location:
                st.session_state["user_location"] = new_location

                # Yeni konumda eski analiz/rotalar geçersiz.
                st.session_state["live_options"] = []
                st.session_state.pop("analysis_signature", None)

                st.session_state["location_status"] = (
                    f"Yeni konum seçildi: "
                    f"{new_location['lat']:.5f}, {new_location['lon']:.5f}"
                )

                # Yeni key ile haritayı ve araç markerını yeniden oluştur.
                st.session_state["map_version"] += 1
                st.rerun()



        st.caption(
            f"Konum: {location['lat']:.5f}, {location['lon']:.5f}"
        )

        if options_to_show:
            st.caption(
                "Renkli markerlar ve rotalar menzil içindeki tüm "
                "istasyonları göstermektedir."
            )

    st.divider()

    if st.button(
        "Analizi Başlat",
        type="primary",
        width="stretch"
    ):
        try:
            selected_location = st.session_state["user_location"]

            live_options = analyze_interactive_location(
                lat=selected_location["lat"],
                lon=selected_location["lon"],
                battery_capacity=profile["battery_kwh"],
                current_soc_percent=current_soc,
                consumption_per_km=profile["consumption_kwh_km"],
                target_soc_percent=target_soc,
                stations=st.session_state["interactive_stations"],
                current_time=0.0,
                min_soc_percent=profile["reserve_soc"]
            )

            st.session_state["live_options"] = live_options

            # İmza, analizin gerçekten kullandığı güncel konumdan oluşsun.
            st.session_state["analysis_signature"] = (
                selected_model,
                current_soc,
                target_soc,
                round(selected_location["lat"], 6),
                round(selected_location["lon"], 6),
            )

            st.session_state["analysis_status"] = (
                f"{len(live_options)} erişilebilir istasyon için analiz tamamlandı."
            )

            st.rerun()

        except ValueError as error:
            st.session_state["live_options"] = []
            st.session_state.pop("analysis_signature", None)
            st.error(str(error))

    current_signature = (
        selected_model,
        current_soc,
        target_soc,
        round(st.session_state["user_location"]["lat"], 6),
        round(st.session_state["user_location"]["lon"], 6),
    )
    active_live_options = []

    if st.session_state.get("analysis_signature") == current_signature:
        active_live_options = st.session_state.get("live_options", [])

    if active_live_options:
        live_df = pd.DataFrame(
            st.session_state["live_options"]
        )

        strategy_column = STRATEGY_MAP[strategy_label]

        recommended_row = live_df.loc[
            live_df[strategy_column].idxmin()
        ]

        st.success(
            f"{strategy_label} önerisi: "
            f"**{recommended_row['station_id']}**"
        )

        st.caption(
            "Aşağıdaki harita ve tablo, seçilen stratejiden bağımsız olarak "
            "menzil içindeki tüm istasyonları gösterir."
        )

        c1, c2, c3, c4 = st.columns(4)

        c1.metric(
            "En Kısa Toplam Süre",
            live_df.loc[
                live_df["total_time"].idxmin(),
                "station_id"
            ]
        )

        c2.metric(
            "En Kısa Mesafe",
            live_df.loc[
                live_df["distance_km"].idxmin(),
                "station_id"
            ]
        )

        c3.metric(
            "En Kısa Bekleme",
            live_df.loc[
                live_df["waiting_time"].idxmin(),
                "station_id"
            ]
        )

        c4.metric(
            "En Kısa Şarj",
            live_df.loc[
                live_df["charging_time"].idxmin(),
                "station_id"
            ]
        )

        st.subheader("Menzil İçindeki Tüm İstasyonlar")

        st.dataframe(
            live_df[
                [
                    "station_id",
                    "charger_power_kw",
                    "distance_km",
                    "travel_time",
                    "waiting_time",
                    "charging_time",
                    "total_time",
                    "soc_at_arrival",
                    "queue_length",
                    "is_best_total_time",
                    "is_best_distance",
                    "is_best_waiting",
                    "is_best_charging",
                ]
            ].rename(
                columns={
                    "charger_power_kw": "Şarj Gücü (kW)",
                    "distance_km": "Mesafe (km)",
                    "travel_time": "Yolculuk (dk)",
                    "waiting_time": "Bekleme (dk)",
                    "charging_time": "Şarj (dk)",
                    "total_time": "Toplam Süre (dk)",
                    "soc_at_arrival": "Varış SoC (%)",
                    "queue_length": "Kuyruk",
                }
            ),
            use_container_width=True
        )


with tab_results:
    left, center = st.columns([1.2, 2])


    with left:
        st.header("Araçlar")

        vehicle_ids = df["vehicle_id"].unique()
        selected_vehicle = st.selectbox("Select vehicle", vehicle_ids)

        vehicle_df = df[df["vehicle_id"] == selected_vehicle]
        vehicle_options = options_df[options_df["vehicle_id"] == selected_vehicle]

        if not vehicle_options.empty:
            latest_time = vehicle_options["time"].max()

            map_options = (
                vehicle_options[vehicle_options["time"] == latest_time]
                .sort_values("total_time_min")
                .drop_duplicates("station_id")
            )
        else:
            map_options = pd.DataFrame()

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

        st.subheader("Menzil İçindeki İstasyon Alternatifleri")

        st.dataframe(
            vehicle_options[
                [
                    "station_id",
                    "distance_km",
                    "travel_time_min",
                    "waiting_time_min",
                    "charging_time_min",
                    "total_time_min",
                    "soc_at_arrival",
                    "is_best_total_time",
                    "is_best_distance",
                    "is_best_waiting",
                    "is_best_charging",
                ]
            ],
            use_container_width=True
        )

        if not map_options.empty:
            route_filter = st.selectbox(
                "Haritada gösterilecek rota",
                ["Tüm rotalar"] + map_options["station_id"].tolist()
            )
        else:
            route_filter = "Tüm rotalar"


        col1, col2, col3, col4 = st.columns(4)

        if vehicle_options.empty:
            st.warning("Bu araç için menzil içi istasyon alternatifi bulunamadı.")
        else:
            col1.metric(
                "En Kısa Toplam Süre",
                vehicle_options.loc[vehicle_options["total_time_min"].idxmin(), "station_id"]
            )

            col2.metric(
                "En Kısa Mesafe",
                vehicle_options.loc[vehicle_options["distance_km"].idxmin(), "station_id"]
            )

            col3.metric(
                "En Kısa Bekleme",
                vehicle_options.loc[vehicle_options["waiting_time_min"].idxmin(), "station_id"]
            )

            col4.metric(
                "En Kısa Şarj",
                vehicle_options.loc[vehicle_options["charging_time_min"].idxmin(), "station_id"]
            )

    with center:
        st.header("Harita / Senaryo Görünümü")

        if map_options.empty:
            st.info("Bu araç için haritada gösterilecek istasyon alternatifi bulunamadı.")

        else:
            first_option = map_options.iloc[0]

            map_center = [
                first_option["vehicle_lat"],
                first_option["vehicle_lon"]
            ]

            m = folium.Map(location=map_center, zoom_start=13)

            folium.Marker(
                [
                    first_option["vehicle_lat"],
                    first_option["vehicle_lon"]
                ],
                popup=f"Araç {selected_vehicle}",
                tooltip=f"Araç {selected_vehicle}",
                icon=folium.Icon(
                    color="red",
                    icon="car",
                    prefix="fa"
                )
            ).add_to(m)

            bounds = [
                [
                    first_option["vehicle_lat"],
                    first_option["vehicle_lon"]
                ]
            ]

            for _, option in map_options.iterrows():
                station_id = option["station_id"]

                badges = []

                if option.get("is_best_total_time", False):
                    badges.append("En kısa toplam süre")

                if option.get("is_best_distance", False):
                    badges.append("En kısa mesafe")

                if option.get("is_best_waiting", False):
                    badges.append("En kısa bekleme")

                if option.get("is_best_charging", False):
                    badges.append("En kısa şarj")

                badge_text = "<br>".join(badges) if badges else "Alternatif istasyon"
                style = get_station_style(option)

                popup_html = f"""
                <b>{station_id}</b><br>
                Mesafe: {option["distance_km"]:.2f} km<br>
                Yolculuk: {option["travel_time_min"]:.2f} dk<br>
                Bekleme: {option["waiting_time_min"]:.2f} dk<br>
                Şarj: {option["charging_time_min"]:.2f} dk<br>
                <b>Toplam: {option["total_time_min"]:.2f} dk</b><br><br>
                {badge_text}
                """

                folium.Marker(
                    location=[
                        option["station_lat"],
                        option["station_lon"]
                    ],
                    popup=folium.Popup(popup_html, max_width=300),
                    tooltip=f'{option["station_id"]} — {style["label"]}',
                    icon=folium.Icon(
                        color=style["color"],
                        icon=style["icon"],
                        prefix="fa"
                    )
                ).add_to(m)

                bounds.append(
                    [
                        option["station_lat"],
                        option["station_lon"]
                    ]
                )

                show_route = (
                    route_filter == "Tüm rotalar"
                    or route_filter == station_id
                )

                if show_route:
                    route_coords = parse_route_coords(option["route_coords"])

                    if route_coords:
                        folium.PolyLine(
                            route_coords,
                            color=style["color"],
                            weight=5,
                            opacity=0.75,
                            tooltip=f'{option["station_id"]} rotası',
                            popup=folium.Popup(popup_html, max_width=300)
                        ).add_to(m)

                        bounds.extend(route_coords)

            if len(bounds) > 1:
                m.fit_bounds(bounds)

            map_data = st_folium(
                m,
                width=900,
                height=600,
                key="station_map"
            )

            clicked_station = map_data.get("last_object_clicked_tooltip")

            if clicked_station:
                st.caption(f"Haritada seçilen öğe: {clicked_station}")
    
    st.divider()

    strategy_summary = build_strategy_summary(options_df)

    if strategy_summary.empty:
        st.info("Strateji karşılaştırması için alternatif istasyon verisi yok.")

    else:
        strategy_summary = build_strategy_summary(options_df)

        if strategy_summary.empty:
            st.info("Strateji karşılaştırması için alternatif istasyon verisi yok.")

        else:
            st.subheader("Strateji Bazlı Ortalama Sonuçlar")

            ordered_strategies = [
                "En kısa toplam süre",
                "En kısa mesafe",
                "En kısa bekleme",
                "En kısa şarj süresi",
            ]

            for strategy_name in ordered_strategies:
                strategy_row = strategy_summary[
                    strategy_summary["Strateji"] == strategy_name
                ]

                if not strategy_row.empty:
                    show_strategy_metrics(strategy_row.iloc[0])
                    st.write("")
            
            st.divider()

            metric_options = {
                "Ortalama toplam süre": "Ort. Toplam Süre (dk)",
                "Ortalama bekleme süresi": "Ort. Bekleme (dk)",
                "Ortalama şarj süresi": "Ort. Şarj (dk)",
                "Ortalama mesafe": "Ort. Mesafe (km)",
                "Beklemesiz seçim oranı": "Beklemesiz Seçim (%)",
            }

            selected_metric_label = st.selectbox(
                "Grafikte gösterilecek metrik",
                list(metric_options.keys()),
                key="strategy_graph_metric"
            )

            selected_metric_column = metric_options[selected_metric_label]

            chart_df = strategy_summary[
                ["Strateji", selected_metric_column]
            ].copy()

            chart_df = chart_df.rename(
                columns={
                    "Strateji": "strategy",
                    selected_metric_column: "value"
                }
            )

            chart_df["value"] = pd.to_numeric(
                chart_df["value"],
                errors="coerce"
            )

            chart_df = chart_df.dropna(subset=["value"])

            strategy_order = [
                "En kısa toplam süre",
                "En kısa mesafe",
                "En kısa bekleme",
                "En kısa şarj süresi",
            ]

            chart = (
                alt.Chart(chart_df)
                .mark_bar()
                .encode(
                    x=alt.X(
                        "strategy:N",
                        title="Strateji",
                        sort=strategy_order,
                        axis=alt.Axis(labelAngle=-15)
                    ),
                    y=alt.Y(
                        "value:Q",
                        title=selected_metric_label,
                        scale=alt.Scale(zero=True)
                    ),
                    tooltip=[
                        alt.Tooltip("strategy:N", title="Strateji"),
                        alt.Tooltip(
                            "value:Q",
                            title=selected_metric_label,
                            format=".2f"
                        ),
                    ],
                )
                .properties(height=380)
            )

            st.altair_chart(
                chart,
                use_container_width=True
            )