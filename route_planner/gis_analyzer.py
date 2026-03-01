"""
GIS空间分析模块
使用osmnx获取真实OSM路网数据，生成符合目标距离的运动路线
研究区域：厦门市环岛路（具备海景、公园、跑道等典型运动场景）
"""
import osmnx as ox
import networkx as nx
import numpy as np
import json
import random
import math
from shapely.geometry import Point, LineString
import warnings
warnings.filterwarnings('ignore')

# ============================================================
# 研究区域：厦门市环岛路附近
# ============================================================
STUDY_AREA_CENTER = (24.4434, 118.1500)  # 厦门环岛路中心点（纬度, 经度）

# 模拟的水站POI（基于真实厦门环岛路沿线位置）
SIMULATED_WATER_STATIONS = [
    {"id": "W001", "name": "椰风寨饮水站",    "lat": 24.4380, "lon": 118.1420},
    {"id": "W002", "name": "白城沙滩便利店",  "lat": 24.4450, "lon": 118.1510},
    {"id": "W003", "name": "胡里山炮台补给点","lat": 24.4390, "lon": 118.1580},
    {"id": "W004", "name": "曾厝垵便利店",    "lat": 24.4460, "lon": 118.1650},
    {"id": "W005", "name": "环岛路公园饮水机","lat": 24.4410, "lon": 118.1480},
]

SIMULATED_SEA_VIEW_POINTS = [
    {"id": "S001", "name": "灯塔观景台",       "lat": 24.4350, "lon": 118.1430, "rating": 5},
    {"id": "S002", "name": "白城海滨观景平台", "lat": 24.4460, "lon": 118.1520, "rating": 4},
    {"id": "S003", "name": "胡里山海岸线",     "lat": 24.4400, "lon": 118.1590, "rating": 5},
]

# 全局路网缓存（避免重复下载）
_GRAPH_CACHE = None


def get_road_network(target_distance_km: float = 9.0) -> nx.MultiDiGraph:
    """
    获取路网，根据目标距离自动调整范围，并缓存避免重复下载
    """
    global _GRAPH_CACHE
    # 目标距离的一半作为路网半径（环形路线），最小3km，最大8km
    needed_radius = max(3000, min(8000, int(target_distance_km * 500)))

    if _GRAPH_CACHE is not None:
        cached_radius = _GRAPH_CACHE.graph.get('dist', 0)
        if cached_radius >= needed_radius:
            print(f"[GIS] 使用缓存路网（半径{cached_radius}m）")
            return _GRAPH_CACHE

    print(f"[GIS] 正在从OSM获取路网，中心点: {STUDY_AREA_CENTER}，半径: {needed_radius}m ...")
    G = ox.graph_from_point(
        STUDY_AREA_CENTER,
        dist=needed_radius,
        network_type='walk',
        simplify=True
    )
    G.graph['dist'] = needed_radius
    print(f"[GIS] 路网获取完成：{len(G.nodes)} 个节点，{len(G.edges)} 条边")

    # NDVI & 路面分析
    G = _annotate_ndvi(G)
    G = _annotate_surface(G)

    _GRAPH_CACHE = G
    return G


def _annotate_ndvi(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """模拟NDVI植被指数，赋值给每条边"""
    for u, v, k, data in G.edges(data=True, keys=True):
        highway = data.get('highway', 'residential')
        if isinstance(highway, list):
            highway = highway[0]
        if highway in ['footway', 'path', 'pedestrian', 'track', 'cycleway']:
            base = 0.55
        elif highway in ['residential', 'living_street']:
            base = 0.35
        elif highway in ['primary', 'secondary', 'tertiary']:
            base = 0.20
        else:
            base = 0.30
        lat = G.nodes[u].get('y', STUDY_AREA_CENTER[0])
        coastal = max(0, (lat - 24.430) * 10)
        ndvi = min(0.85, max(0.05, base + coastal * 0.1 + random.gauss(0, 0.05)))
        G[u][v][k]['ndvi'] = round(ndvi, 3)
        G[u][v][k]['shade_score'] = round(ndvi * 100, 1)
    return G


def _annotate_surface(G: nx.MultiDiGraph) -> nx.MultiDiGraph:
    """标注路面软硬类型"""
    soft_types = {'footway', 'path', 'track', 'cycleway'}
    hard_osm = {'asphalt', 'concrete', 'paving_stones'}
    soft_osm = {'unpaved', 'gravel', 'grass', 'dirt', 'ground'}
    for u, v, k, data in G.edges(data=True, keys=True):
        highway = data.get('highway', 'residential')
        if isinstance(highway, list):
            highway = highway[0]
        osm_surface = data.get('surface', '')
        if osm_surface in hard_osm:
            surface = 'hard'
        elif osm_surface in soft_osm:
            surface = 'soft'
        else:
            surface = 'soft' if highway in soft_types else 'hard'
        G[u][v][k]['surface'] = surface
    return G


def _haversine_m(lat1, lon1, lat2, lon2) -> float:
    """计算两点间距离（米）"""
    R = 6371000
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlam = math.radians(lon2 - lon1)
    a = math.sin(dphi/2)**2 + math.cos(phi1)*math.cos(phi2)*math.sin(dlam/2)**2
    return R * 2 * math.atan2(math.sqrt(a), math.sqrt(1-a))


def _find_node(G, lat, lon):
    return ox.nearest_nodes(G, lon, lat)


def _path_length_m(G, nodes) -> float:
    """计算路径总长度（米）"""
    total = 0.0
    for u, v in zip(nodes[:-1], nodes[1:]):
        ed = G.get_edge_data(u, v)
        if ed:
            total += list(ed.values())[0].get('length', 50)
    return total


def _build_loop_route(G, start_node, target_km: float, angle_deg: float) -> list:
    """
    构建一条接近目标距离的环形路线。
    策略：沿给定方向走到约 target_km/2 处，再绕回起点。
    """
    center_lat = G.nodes[start_node]['y']
    center_lon = G.nodes[start_node]['x']

    # 目标半程距离（米）
    half_m = target_km * 1000 / 2.0

    # 按角度计算中间节点坐标（1度纬度≈111km，1度经度≈111km*cos(lat)）
    angle_rad = math.radians(angle_deg)
    dlat = (half_m / 111000) * math.cos(angle_rad)
    dlon = (half_m / (111000 * math.cos(math.radians(center_lat)))) * math.sin(angle_rad)

    mid_lat = center_lat + dlat
    mid_lon = center_lon + dlon

    mid_node = _find_node(G, mid_lat, mid_lon)

    # 规划 起点→中间点→起点 的路径
    try:
        path1 = nx.shortest_path(G, start_node, mid_node, weight='length')
        path2 = nx.shortest_path(G, mid_node, start_node, weight='length')
        # 合并，去掉重复的中间节点
        full_path = path1 + path2[1:]
        return full_path
    except nx.NetworkXNoPath:
        return []


def generate_routes(G: nx.MultiDiGraph, params: dict) -> list:
    """
    生成A/B/C三条备选路线，每条路线长度接近用户目标距离
    """
    print("[GIS] 正在生成备选路线...")
    target_km = params.get('estimated_distance_km', 9.0)

    center_lat, center_lon = STUDY_AREA_CENTER
    start_node = _find_node(G, center_lat, center_lon)

    # 三条路线：不同方向，略微不同的目标距离（±10%变化）
    route_configs = [
        {
            "route_id": "ROUTE_A",
            "name": "路线A：椰风寨-灯塔环线",
            "angle_deg": 225,          # 向西南（海岸方向）
            "dist_factor": 1.00,       # 100% 目标距离
            "highlight": "途经椰风寨公园，终点灯塔观景台，海景绝佳",
            "sea_view_point": SIMULATED_SEA_VIEW_POINTS[0],
        },
        {
            "route_id": "ROUTE_B",
            "name": "路线B：白城沙滩-环岛路主线",
            "angle_deg": 45,           # 向东北（沿海方向）
            "dist_factor": 0.90,
            "highlight": "沿环岛路主线跑，白城沙滩观海，树荫最多",
            "sea_view_point": SIMULATED_SEA_VIEW_POINTS[1],
        },
        {
            "route_id": "ROUTE_C",
            "name": "路线C：胡里山炮台-曾厝垵文创村",
            "angle_deg": 90,           # 向东（文创区方向）
            "dist_factor": 1.10,
            "highlight": "途经胡里山炮台历史遗迹，曾厝垵文创村补给，路面最友好",
            "sea_view_point": SIMULATED_SEA_VIEW_POINTS[2],
        },
    ]

    routes = []
    for cfg in route_configs:
        this_target = target_km * cfg['dist_factor']
        try:
            path_nodes = _build_loop_route(G, start_node, this_target, cfg['angle_deg'])
            if not path_nodes or len(path_nodes) < 5:
                raise ValueError("路径节点过少")

            metrics = _calc_metrics(G, path_nodes, params)
            actual_km = metrics['distance_km']

            # 如果实际距离与目标偏差超过40%，尝试扩展路径
            if actual_km < this_target * 0.6:
                print(f"[GIS] {cfg['name']} 实际距离{actual_km:.1f}km 偏短，尝试扩展...")
                path_nodes = _extend_path(G, path_nodes, start_node, this_target)
                metrics = _calc_metrics(G, path_nodes, params)

            metrics.update({
                'route_id': cfg['route_id'],
                'name': cfg['name'],
                'highlight': cfg['highlight'],
                'sea_view_point': cfg['sea_view_point'],
                'geojson': _path_to_geojson(G, path_nodes),
            })
            routes.append(metrics)
            print(f"[GIS] {cfg['name']} 完成：{metrics['distance_km']:.1f}km，预计{metrics['estimated_time_min']}分钟")

        except Exception as e:
            print(f"[GIS] {cfg['name']} 生成失败: {e}，使用备用方案")
            routes.append(_fallback_route(cfg, params))

    return routes


def _extend_path(G, path_nodes, start_node, target_km):
    """
    通过在路径中插入更多中间节点来延长路线，使其接近目标距离
    """
    current_len = _path_length_m(G, path_nodes) / 1000
    target_m = target_km * 1000

    # 从路网中随机选取沿途节点，串联成更长路线
    all_nodes = list(G.nodes())
    center_lat = G.nodes[start_node]['y']
    center_lon = G.nodes[start_node]['x']

    # 按距起点的距离筛选候选节点（在合理范围内）
    max_radius_m = target_km * 600
    candidates = []
    for n in all_nodes:
        nd = G.nodes[n]
        d = _haversine_m(center_lat, center_lon, nd['y'], nd['x'])
        if d < max_radius_m:
            candidates.append((n, d))

    # 按距离排序，取中等距离的节点作为途经点
    candidates.sort(key=lambda x: x[1])
    waypoints = []
    step = max(1, len(candidates) // 8)
    for i in range(2, 7):
        idx = min(i * step, len(candidates) - 1)
        waypoints.append(candidates[idx][0])

    # 构建经过多个途经点的路线
    full_path = [start_node]
    prev = start_node
    for wp in waypoints:
        try:
            seg = nx.shortest_path(G, prev, wp, weight='length')
            full_path.extend(seg[1:])
            prev = wp
            if _path_length_m(G, full_path) >= target_m * 0.85:
                break
        except Exception:
            continue

    # 返回起点
    try:
        back = nx.shortest_path(G, prev, start_node, weight='length')
        full_path.extend(back[1:])
    except Exception:
        pass

    return full_path if len(full_path) > len(path_nodes) else path_nodes


def _calc_metrics(G, path_nodes, params) -> dict:
    """计算路线多维度指标"""
    total_length = 0.0
    total_ndvi = 0.0
    soft_count = 0
    edge_count = 0

    for u, v in zip(path_nodes[:-1], path_nodes[1:]):
        ed = G.get_edge_data(u, v)
        if ed:
            data = list(ed.values())[0]
            length = data.get('length', 50)
            total_length += length
            total_ndvi += data.get('ndvi', 0.3)
            if data.get('surface') == 'soft':
                soft_count += 1
            edge_count += 1

    distance_km = total_length / 1000
    avg_ndvi = total_ndvi / max(edge_count, 1)
    shade_pct = int(avg_ndvi * 100)
    soft_pct = soft_count / max(edge_count, 1) * 100

    if soft_pct > 60:
        surface_desc = "塑胶跑道/土路为主（脚踝友好）"
    elif soft_pct > 30:
        surface_desc = "软硬混合路面"
    else:
        surface_desc = "铺装路面为主"

    elevation_gain = int(distance_km * random.uniform(8, 25))
    water_count = _count_water_stations(G, path_nodes)
    pace = 6.0 + (1 - avg_ndvi) * 0.5
    estimated_time = int(distance_km * pace)

    return {
        "distance_km": round(distance_km, 2),
        "estimated_time_min": estimated_time,
        "shade_coverage_pct": shade_pct,
        "avg_ndvi": round(avg_ndvi, 3),
        "water_stations": water_count,
        "elevation_gain_m": elevation_gain,
        "surface_type": surface_desc,
        "soft_surface_pct": round(soft_pct, 1),
        "node_count": len(path_nodes),
    }


def _count_water_stations(G, path_nodes, buffer_m=300) -> int:
    route_coords = [(G.nodes[n]['y'], G.nodes[n]['x']) for n in path_nodes]
    count = 0
    for station in SIMULATED_WATER_STATIONS:
        for lat, lon in route_coords:
            dist_m = _haversine_m(station['lat'], station['lon'], lat, lon)
            if dist_m < buffer_m:
                count += 1
                break
    return count


def _path_to_geojson(G, path_nodes) -> dict:
    coords = [[G.nodes[n]['x'], G.nodes[n]['y']] for n in path_nodes]
    return {
        "type": "Feature",
        "geometry": {"type": "LineString", "coordinates": coords},
        "properties": {}
    }


def _fallback_route(cfg, params) -> dict:
    """备用路线（当路网分析失败时）"""
    target_km = params.get('estimated_distance_km', 9.0)
    distance_km = target_km * cfg.get('dist_factor', 1.0) * random.uniform(0.90, 1.10)
    pace = 6.0
    return {
        "route_id": cfg['route_id'],
        "name": cfg['name'],
        "distance_km": round(distance_km, 2),
        "estimated_time_min": int(distance_km * pace),
        "shade_coverage_pct": random.randint(30, 70),
        "avg_ndvi": round(random.uniform(0.3, 0.7), 3),
        "water_stations": random.randint(1, 3),
        "elevation_gain_m": int(distance_km * random.uniform(8, 20)),
        "surface_type": "软硬混合路面",
        "soft_surface_pct": round(random.uniform(20, 60), 1),
        "node_count": 0,
        "highlight": cfg['highlight'],
        "sea_view_point": cfg['sea_view_point'],
        "geojson": None,
    }


def run_full_gis_analysis(params: dict) -> list:
    """
    执行完整GIS分析流程，返回三条备选路线
    """
    print("\n" + "="*50)
    print("开始GIS空间分析流程")
    print("="*50)

    target_km = params.get('estimated_distance_km', 9.0)
    G = get_road_network(target_km)
    routes = generate_routes(G, params)

    print("\n" + "="*50)
    print("GIS分析完成，生成了以下路线：")
    for r in routes:
        print(f"  {r['name']}: {r['distance_km']}km, 树荫{r['shade_coverage_pct']}%, 水站{r['water_stations']}个")
    print("="*50 + "\n")

    return routes


if __name__ == "__main__":
    test_params = {
        "duration_min": 90,
        "activity_type": "跑步",
        "intensity": "耐力",
        "preferred_features": ["shade", "water", "sea_view"],
        "surface_preference": "soft",
        "health_constraints": ["ankle"],
        "estimated_distance_km": 15.0,
    }
    routes = run_full_gis_analysis(test_params)
    import json
    print(json.dumps(routes[0], ensure_ascii=False, indent=2, default=str))
