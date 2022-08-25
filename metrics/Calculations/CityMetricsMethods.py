import geopandas as gpd
import shapely
import pandas as pd
import math
import json
import numpy as np
import shapely.wkt
import ast
import io
import pca

from shapely.geometry import Polygon
from jsonschema.exceptions import ValidationError
from .utils import routes_between_two_points
from scipy.cluster.hierarchy import linkage, dendrogram, fcluster
from sklearn.cluster import KMeans
from sklearn.preprocessing import StandardScaler
from matplotlib import pyplot as plt

class BaseMethod():

    def __init__(self, city_model):

        self.city_model = city_model
        self.city_crs = city_model.city_crs
        self.mode = city_model.mode

    def validation(self, method):
        if self.mode == "user_mode":
            if not self.city_model.methods.if_method_available(method):
                bad_layers = self.city_model.methods.get_bad_layers(method)
                raise ValidationError(f'Layers {", ".join(bad_layers)} do not match specification.')

    @staticmethod
    def get_territorial_select(area_type, area_id, *args):
        return tuple(df[df[area_type + "_id"] == area_id] for df in args)

    @staticmethod
    def get_custom_polygon_select(geojson, set_crs, *args):

        geojson_crs = geojson["crs"]["properties"]["name"]
        geojson = gpd.GeoDataFrame.from_features(geojson['features'])
        geojson = geojson.set_crs(geojson_crs).to_crs(set_crs)
        custom_polygon = geojson['geometry'][0]
        return tuple(df[df.within(custom_polygon)] for df in args)

    # TODO: add method for slicing object's dataframe with specifed parameter

# ########################################  Trafiics calculation  ####################################################
class TrafficCalculator(BaseMethod):

    def __init__(self, city_model):

        BaseMethod.__init__(self, city_model)
        super().validation("traffic_calculator")
        self.stops = self.city_model.Public_Transport_Stops.copy()
        self.buildings = self.city_model.Buildings.copy()
        self.walk_graph = self.city_model.walk_graph.copy()

    def get_trafic_calculation(self, request_area_geojson):

        living_buildings = self.buildings[self.buildings['population'] > 0]
        living_buildings = living_buildings[['id', 'population', 'geometry']]
        selected_buildings = self.get_custom_polygon_select(request_area_geojson, self.city_crs, living_buildings)[0]

        if len(selected_buildings) == 0:
            return None
        
        stops = self.stops.set_index("id")
        selected_buildings['nearest_stop_id'] = selected_buildings.apply(
            lambda x: stops['geometry'].distance(x['geometry']).idxmin(), axis=1)
        nearest_stops = stops.loc[list(selected_buildings['nearest_stop_id'])]
        path_info = selected_buildings.apply(
            lambda x: routes_between_two_points(graph=self.walk_graph, weight="length",
            p1 = x['geometry'].centroid.coords[0], p2 = stops.loc[x['nearest_stop_id']].geometry.coords[0]), 
            result_type="expand", axis=1)
        house_stop_routes = selected_buildings.copy().drop(["geometry"], axis=1).join(path_info)

        # 30% aprox value of Public transport users
        house_stop_routes['population'] = (house_stop_routes['population'] * 0.3).round().astype("int")
        house_stop_routes = house_stop_routes.rename(
            columns={'population': 'route_traffic', 'id': 'building_id', "route_geometry": "geometry"})
        house_stop_routes = house_stop_routes.set_crs(selected_buildings.crs)

        return {"buildings": json.loads(selected_buildings.reset_index(drop=True).to_crs(4326).to_json()), 
                "stops": json.loads(nearest_stops.reset_index(drop=True).to_crs(4326).to_json()), 
                "routes": json.loads(house_stop_routes.reset_index(drop=True).to_crs(4326).to_json())}

# ########################################  Visibility analysis  ####################################################

class VisibilityAnalysis(BaseMethod):

    def __init__(self, city_model):
        BaseMethod.__init__(self, city_model)
        super().validation("traffic_calculator")
        self.buildings = self.city_model.Buildings.copy()

    def get_visibility_result(self, point, view_distance):
        
        point_buffer = shapely.geometry.Point(point).buffer(view_distance)
        s = self.buildings.within(point_buffer)
        buildings_in_buffer = self.buildings.loc[s[s].index].reset_index(drop=True)
        buffer_exterior_ = list(point_buffer.exterior.coords)
        line_geometry = [shapely.geometry.LineString([point, ext]) for ext in buffer_exterior_]
        buffer_lines_gdf = gpd.GeoDataFrame(geometry=line_geometry)
        united_buildings = buildings_in_buffer.unary_union

        if united_buildings:
            splited_lines = buffer_lines_gdf.apply(lambda x: x['geometry'].difference(united_buildings), axis=1)
        else:
            splited_lines = buffer_lines_gdf["geometry"]

        splited_lines_gdf = gpd.GeoDataFrame(geometry=splited_lines).explode()
        splited_lines_list = []

        for u, v in splited_lines_gdf.groupby(level=0):
            splited_lines_list.append(v.iloc[0]['geometry'].coords[-1])
        circuit = shapely.geometry.Polygon(splited_lines_list)
        if united_buildings:
            circuit = circuit.difference(united_buildings)

        view_zone = gpd.GeoDataFrame(geometry=[circuit]).set_crs(self.city_crs).to_crs(4326)
        return json.loads(view_zone.to_json())

# ########################################  Weighted Voronoi  ####################################################
class WeightedVoronoi(BaseMethod):

    def __init__(self, city_model):
        BaseMethod.__init__(self, city_model)

    @staticmethod
    def self_weight_list_calculation(start_value, iter_count): 
        log_r = [start_value]
        self_weigth =[]
        max_value = log_r[0] * iter_count
        for i in range(iter_count):
            next_value = log_r[-1] + math.log(max_value / log_r[-1], 1.5)
            log_r.append(next_value)
            self_weigth.append(log_r[-1] - log_r[i])
        return self_weigth, log_r

    @staticmethod
    def vertex_checker(x_coords, y_coords, growth_rules, encounter_indexes, input_geojson):
        for i in range(len(growth_rules)):
            if growth_rules[i] == False:
                pass
            else:
                for index in encounter_indexes:
                    if shapely.geometry.Point(x_coords[i],y_coords[i]).within(input_geojson['geometry'][index]):
                        growth_rules[i] = False
                        break
        return growth_rules

    @staticmethod
    def growth_funtion_x(x_coords, growth_rules, iteration_weight):
        growth_x = [x_coords[i-1] + iteration_weight  *math.sin(2 * math.pi * i / 65) 
        if growth_rules[i-1] == True else x_coords[i-1] for i in range(1, len(x_coords) + 1)]
        return growth_x 
    
    @staticmethod
    def growth_funtion_y(y_coords, growth_rules, iteration_weight):    
        growth_y = [y_coords[i-1] + iteration_weight * math.cos(2 * math.pi * i / 65) 
        if growth_rules[i-1] == True else y_coords[i-1] for i in range(1, len(y_coords) + 1)]
        return growth_y

    def get_weighted_voronoi_result(self, geojson):

        iter_count = 300
        geojson_crs = geojson["crs"]["properties"]["name"]
        input_geojson = gpd.GeoDataFrame.from_features(geojson['features']).set_crs(geojson_crs)
        input_geojson['init_centroid'] = input_geojson.apply(lambda x: list(x['geometry'].coords)[0], axis = 1)
        input_geojson['geometry'] = input_geojson.apply(lambda x: shapely.geometry.Polygon([
            [list(x['geometry'].coords)[0][0] + x['weight'] * math.sin(2 * math.pi * i / 65),
            list(x['geometry'].coords)[0][1] + x['weight'] * math.cos(2 * math.pi * i / 65)] 
            for i in range(1, 65)]), axis =1)
        input_geojson['x'] = input_geojson.apply(
            lambda x: list(list(zip(*list(x['geometry'].exterior.coords)))[0]), axis = 1)
        input_geojson['y'] = input_geojson.apply(
            lambda x: list(list(zip(*list(x['geometry'].exterior.coords)))[1]), axis = 1)
        input_geojson['self_weight'] = input_geojson.apply(
            lambda x: self.self_weight_list_calculation(x['weight'], iter_count)[0], axis = 1)
        input_geojson['self_radius'] = input_geojson.apply(
            lambda x: self.self_weight_list_calculation(x['weight'], iter_count)[1], axis = 1)
        input_geojson['vertex_growth_allow_rule'] = input_geojson.apply(
            lambda x: [True for x in range(len(x['x']))], axis = 1)
        temp = pd.DataFrame({'x':input_geojson.apply(
            lambda x: self.growth_funtion_x(x['x'], x['vertex_growth_allow_rule'],x['self_radius'][-1]), axis = 1),
                    'y':input_geojson.apply(
                        lambda x: self.growth_funtion_y(x['y'], x['vertex_growth_allow_rule'], x['self_radius'][-1]), 
                        axis = 1)}).apply(
                            lambda x: shapely.geometry.Polygon(tuple(zip(x['x'], x['y']))), axis = 1)
        input_geojson['encounter_rule_index'] = [
            [y for y in range(len(temp)) if y != x if temp[x].intersects(temp[y])] for x in range(len(temp))]
        for i in range(iter_count):
            input_geojson['x'] = input_geojson.apply(
                lambda x: self.growth_funtion_x(x['x'], x['vertex_growth_allow_rule'],x['self_weight'][i]), axis = 1)
            input_geojson['y'] = input_geojson.apply(
                lambda x: self.growth_funtion_y(x['y'],x['vertex_growth_allow_rule'],x['self_weight'][i]), axis = 1)
            input_geojson['geometry'] = input_geojson.apply(
                lambda x: shapely.geometry.Polygon(tuple(zip(x['x'], x['y']))), axis = 1)   
            input_geojson['vertex_growth_allow_rule'] = input_geojson.apply(
                lambda x: self.vertex_checker(
                    x['x'], x['y'], x['vertex_growth_allow_rule'], x['encounter_rule_index'], input_geojson), 
                    axis = 1)
        
        start_points = gpd.GeoDataFrame.from_features(geojson['features'])
        x = [list(p.coords)[0][0] for p in start_points['geometry']]
        y = [list(p.coords)[0][1] for p in start_points['geometry']]
        centroid = shapely.geometry.Point(
            (sum(x) / len(start_points['geometry']), sum(y) / len(start_points['geometry'])))
        buffer_untouch = centroid.buffer(start_points.distance(shapely.geometry.Point(centroid)).max()*1.4)
        buffer_untouch = gpd.GeoDataFrame(data = {'id':[1]} ,geometry = [buffer_untouch]).set_crs(3857)
        
        result = gpd.overlay(buffer_untouch, input_geojson, how='difference')
        input_geojson = input_geojson.to_crs(4326)
        result = result.to_crs(4326)
        return {'voronoi_polygons': json.loads(input_geojson[['weight','geometry']].to_json()),
                'deficit_zones': json.loads(result.to_json())}

# ########################################  Blocks clusterization  ###################################################
class BlocksClusterization(BaseMethod):
    def __init__(self, city_model):
        BaseMethod.__init__(self, city_model)
        super().validation("blocks_clusterization")
        self.services = self.city_model.Services.copy()
        self.blocks = self.city_model.Blocks.copy()
    
    def clusterize(self, service_types):

        service_in_blocks = self.services.groupby(["block_id", "service_code"])["id"].count().unstack(fill_value=0)
        without_services = self.blocks["id"][~self.blocks["id"].isin(service_in_blocks.index)].values
        without_services = pd.DataFrame(columns=service_in_blocks.columns, index=without_services).fillna(0)
        service_in_blocks = pd.concat([without_services, service_in_blocks])

        service_in_blocks = service_in_blocks[service_types]
        clusterization = linkage(service_in_blocks, method="ward")

        return clusterization, service_in_blocks

    @staticmethod
    def get_clusters_number(clusterization):

        distance = clusterization[-100:, 2]
        clusters = np.arange(1, len(distance) + 1)
        acceleration = np.diff(distance, 2)[::-1]
        series_acceleration = pd.Series(acceleration, index=clusters[:-2] + 1)

        # There are always more than two clusters
        series_acceleration = series_acceleration.iloc[1:]
        clusters_number = series_acceleration.idxmax()

        return clusters_number

    def get_blocks(self, service_types, clusters_number=None, area_type=None, area_id=None, geojson=None):

        clusterization, service_in_blocks = self.clusterize(service_types)
        
        # If user doesn't specified the number of clusters, use default value.
        # The default value is determined with the rate of change in the distance between clusters
        if not clusters_number:
            clusters_number = self.get_clusters_number(clusterization)

        service_in_blocks["cluster_labels"] = fcluster(clusterization, t=int(clusters_number), criterion="maxclust")
        blocks = self.blocks.join(service_in_blocks, on="id")
        mean_services_number = service_in_blocks.groupby("cluster_labels")[service_types].mean().round()
        mean_services_number = service_in_blocks[["cluster_labels"]].join(mean_services_number, on="cluster_labels")
        deviations_services_number = service_in_blocks[service_types] - mean_services_number[service_types]
        blocks = blocks.join(deviations_services_number, on="id", rsuffix="_deviation")

        if area_type and area_id:
            blocks = self.get_territorial_select(area_type, area_id, blocks)[0]
        elif geojson:
            blocks = self.get_custom_polygon_select(geojson, self.city_crs, blocks)[0]

        return json.loads(blocks.to_crs(4326).to_json())

    def get_dendrogram(self, service_types):
            
            clusterization, service_in_blocks = self.clusterize(service_types)

            img = io.BytesIO()
            plt.figure(figsize=(20, 10))
            plt.title("Dendrogram")
            plt.xlabel("Distance")
            plt.ylabel("Block clusters")
            dn = dendrogram(clusterization, p=7, truncate_mode="level")
            plt.savefig(img, format="png")
            plt.close()
            img.seek(0)

            return img

# ########################################  Services clusterization  #################################################
class ServicesClusterization(BaseMethod):
    def __init__(self, city_model):
        BaseMethod.__init__(self, city_model)
        super().validation("services_clusterization")
        self.services = self.city_model.Services.copy()
    
    @staticmethod
    def get_service_cluster(services_select, condition, condition_value):
        services_coords = pd.DataFrame({"x": services_select.geometry.x, "y": services_select.geometry.y})
        clusterization = linkage(services_coords.to_numpy(), method="ward")
        services_select["cluster"] = fcluster(clusterization, t=condition_value, criterion=condition)
        return services_select

    @staticmethod
    def find_dense_groups(loc, n_std):
        if len(loc) > 1:
            X = pd.DataFrame({"x": loc.x, "y": loc.y})
            X = X.to_numpy()
            outlier = pca.spe_dmodx(X, n_std=n_std)[0]["y_bool_spe"]
            return pd.Series(data=outlier.values, index=loc.index)
        else:
            return pd.Series(data=True, index=loc.index)

    @staticmethod
    def get_service_ratio(loc):
        all_services = loc["id"].count()
        services_count = loc.groupby("service_code")["id"].count()
        return (services_count / all_services).round(2)

    def get_clusters_polygon(self, service_types, area_type = None, area_id = None, geojson = None, 
                            condition="distance", condition_value=4000, n_std = 2):

        services_select = self.services[self.services["service_code"].isin(service_types)]
        if area_type and area_id:
            services_select = self.get_territorial_select(area_type, area_id, services_select)[0]
        elif geojson:
            services_select = self.get_custom_polygon_select(geojson, self.city_crs, services_select)[0]
        if len(services_select) <= 1:
            return None

        services_select = self.get_service_cluster(services_select, condition, condition_value)

        # Find outliers of clusters and exclude it
        outlier = services_select.groupby("cluster")["geometry"].apply(lambda x: self.find_dense_groups(x, n_std))
        cluster_normal = 0
        if any(~outlier):
            services_normal = services_select[~outlier]

            if len(services_normal) > 0:
                cluster_service = services_normal.groupby(["cluster"]).apply(lambda x: self.get_service_ratio(x))
                if isinstance(cluster_service, pd.Series):
                    cluster_service = cluster_service.unstack(level=1, fill_value=0)

                # Get MultiPoint from cluster Points and make polygon
                polygons_normal = services_normal.dissolve("cluster").convex_hull
                df_clusters_normal = pd.concat([cluster_service, polygons_normal.rename("geometry")], axis=1
                                                ).reset_index(drop=True)
                cluster_normal = df_clusters_normal.index.max()
        else:
            df_clusters_normal = None

        # Select outliers 
        if any(outlier):
            services_outlier = services_select[outlier]

            # Reindex clusters
            clusters_outlier = cluster_normal + 1
            new_clusters = [c for c in range(clusters_outlier, clusters_outlier + len(services_outlier))]
            services_outlier["cluster"] = new_clusters
            cluster_service = services_outlier.groupby(["cluster"]).apply(lambda x: self.get_service_ratio(x))
            if isinstance(cluster_service, pd.Series):
                cluster_service = cluster_service.unstack(level=1, fill_value=0)
            df_clusters_outlier = cluster_service.join(services_outlier.set_index("cluster")["geometry"])
        else:
            df_clusters_outlier = None

        df_clusters = pd.concat([df_clusters_normal, df_clusters_outlier]).fillna(0).set_geometry("geometry")
        df_clusters["geometry"] = df_clusters["geometry"].buffer(50, join_style=3)
        df_clusters = df_clusters.reset_index().rename(columns={"index": "cluster_id"})

        df_clusters = df_clusters.set_crs(self.city_crs).to_crs(4326)

        return json.loads(df_clusters.to_json())

# #############################################  Spacematrix  #######################################################
class Spacematrix(BaseMethod):
    def __init__(self, city_model):
        BaseMethod.__init__(self, city_model)
        super().validation("spacematrix")
        self.buildings = self.city_model.Buildings.copy()
        self.blocks = self.city_model.Blocks.copy().set_index("id")

    @staticmethod
    def simple_preprocess_data(buildings, blocks):

        # temporary filters. since there are a few bugs in buildings table from DB
        buildings = buildings[buildings["block_id"].notna()]
        buildings = buildings[buildings["storeys_count"].notna()]
        buildings["is_living"] = buildings["is_living"].fillna(False)

        buildings["building_area"] = buildings["basement_area"] * buildings["storeys_count"]
        bad_living_area = buildings[buildings["living_area"] > buildings["building_area"]].index
        buildings.loc[bad_living_area, "living_area"] = None

        living_grouper = buildings.groupby(["is_living"])
        buildings["living_area"] = living_grouper.apply(
            lambda x: x.living_area.fillna(x.building_area * 0.8) if x.name else x.living_area.fillna(0)
            ).droplevel(0).round(2)

        blocks_area_nans = blocks[blocks["area"].isna()].index
        blocks.loc[blocks_area_nans, "area"] = blocks["geometry"].loc[blocks_area_nans].area

        return buildings, blocks

    @staticmethod
    def calculate_block_indices(buildings, blocks):

        sum_grouper = buildings.groupby(["block_id"]).sum()
        blocks["FSI"] = sum_grouper["building_area"] / blocks["area"]
        blocks["GSI"] = sum_grouper["basement_area"] / blocks["area"]
        blocks["MXI"] = (sum_grouper["living_area"] / sum_grouper["building_area"]).round(2)
        blocks["L"] =( blocks["FSI"] / blocks["GSI"]).round()
        blocks["OSR"] = ((1 - blocks["GSI"]) / blocks["FSI"]).round(2)
        blocks[["FSI", "GSI"]] = blocks[["FSI", "GSI"]].round(2)

        return blocks

    @staticmethod
    def name_spacematrix_morph_types(cluster):

        ranges = [[0, 3, 6, 10, 17], 
                  [0, 1, 2], 
                  [0, 0.22, 0.55]]

        labels = [["Малоэтажный", "Среднеэтажный", "Повышенной этажности", "Многоэтажный", "Высотный"],
                  [" низкоплотный", "", " плотный"], 
                  [" нежилой", " смешанный", " жилой"]]

        cluster_name = []
        for ind in range(len(cluster)):
            cluster_name.append(
                labels[ind][[i for i in range(len(ranges[ind])) if cluster.iloc[ind] >= ranges[ind][i]][-1]]
                )
        return "".join(cluster_name)


    def get_spacematrix_morph_types(self, clusters_number=11, area_type=None, area_id=None, geojson=None):

        buildings, blocks = self.simple_preprocess_data(self.buildings, self.blocks)
        blocks = self.calculate_block_indices(buildings, blocks)

        # blocks with OSR >=10 considered as unbuilt blocks
        X = blocks[blocks["OSR"] < 10][['FSI', 'L', 'MXI']].dropna()
        scaler = StandardScaler()
        X_scaler = pd.DataFrame(scaler.fit_transform(X))
        kmeans = KMeans(n_clusters=clusters_number, random_state=42).fit(X_scaler)
        X["spacematrix_cluster"] = kmeans.labels_
        blocks = blocks.join(X["spacematrix_cluster"])
        cluster_grouper = blocks.groupby(["spacematrix_cluster"]).median()
        named_clusters = cluster_grouper[["L", "FSI", "MXI"]].apply(
            lambda x: self.name_spacematrix_morph_types(x), axis=1)
        blocks = blocks.join(named_clusters.rename("spacematrix_morphotype"), on="spacematrix_cluster")

        if area_type and area_id:
            blocks = self.get_territorial_select(area_type, area_id, blocks)[0]
        elif geojson:
            blocks = self.get_custom_polygon_select(geojson, self.city_crs, blocks)[0]

        return json.loads(blocks.to_crs(4326).to_json())

# ######################################### Accessibility isochrones #################################################
class AccessibilityIsochrones(BaseMethod):
    def __init__(self, city_model):
        BaseMethod.__init__(self, city_model)
        super().validation("isochrone")
        self.intermodal_graph = self.city_model.intermodal_graph.copy()


class City_Metrics_Methods():

    def __init__(self, cities_model, cities_crs):

        self.cities_inf_model = cities_model
        self.cities_crs = cities_crs

    # ######################################### Wellbeing ##############################################
    def get_wellbeing(self, BCAM, living_situation_id=None, user_service_types=None, area=None,
                      provision_type="calculated", city="Saint_Petersburg", wellbeing_option=None, return_dfs=False):
        """
        :param BCAM: class containing get_provision function --> class
        :param living_situation_id: living situation id from DB --> int (default None)
        :param city: city to chose data and projection --> str
        :param area: dict that contains area type as key and area index (or geometry) as value --> int or FeatureCollection
        :param user_service_types: use to define own set of services and their coefficient --> dict (default None)
                with service types as keys and coefficients as values
        :param wellbeing_option: option that define which houses are viewed on map --> list of int (default None)
                                given list include left and right boundary
        :param provision_type: define provision calculation method --> str
                "calculated" - provision based on calculated demand, "normative" - provision based on normative demand

        :return: dict containing FeatureCollection of houses and FeatureCollection of services.
                houses properties - id, address, population, wellbeing evaluation
                services properties - id, address, service type, service_name, total demand, capacity
        """

        # Get service coefficient from DB or user request
        service_coef = self.parse_service_coefficients(user_service_types, living_situation_id)
        if type(service_coef) is tuple:
            return service_coef

        provision = BCAM.get_provision(list(service_coef["service_code"]), area, provision_type)
        if type(provision) is tuple:
            return provision

        houses = gpd.GeoDataFrame.from_features(provision["houses"]).set_crs(4326)
        services = gpd.GeoDataFrame.from_features(provision["services"]).set_crs(4326)
        provision_columns = houses.filter(regex="provision").replace("None", np.nan)
        unprovided_columns = list(houses.filter(regex="unprovided").columns)

        available_service_type = [t.split("_provision")[0] for t in provision_columns.columns]
        service_coef = service_coef[service_coef["service_code"].isin(available_service_type)]
        provision_columns = provision_columns.reindex(sorted(provision_columns.columns), axis=1)
        weighted_provision_columns = provision_columns * service_coef.set_axis(provision_columns.columns)["evaluation"]
        houses["mean_provision"] = weighted_provision_columns.apply(
            lambda x: x.mean() if len(x[x.notna()]) > 0 else None, axis=1)
        wellbeing = self.calculate_wellbeing(provision_columns, service_coef)
        houses = houses.drop(list(houses.filter(regex="demand").columns) + unprovided_columns +
                             list(houses.filter(regex="available").columns), axis=1).join(wellbeing)

        if wellbeing_option:
            houses = houses[houses["wellbeing"].between(*wellbeing_option)]
            # PLUG!!! There must be slice by functional object id for services

        if return_dfs:
            return {"houses": houses.to_crs(4326), "services": services.to_crs(4326)}

        return {"houses": eval(houses.reset_index().fillna("None").to_crs(4326).to_json()),
                "services": eval(services.reset_index().fillna("None").to_crs(4326).to_json())}

    def get_wellbeing_info(self, BCAM, object_type, functional_object_id, provision_type="calculated",
                           living_situation_id=None, user_service_types=None, city="Saint_Petersburg"):
        """
        :param BCAM: class containing get_provision function --> class
        :param object_type: house or service --> str
        :param functional_object_id: house or service id from DB --> int
        :param provision_type: provision_type: define provision calculation method --> str
                "calculated" - provision based on calculated demand, "normative" - provision based on normative demand
        :param living_situation_id: living situation id from DB --> int (default None)
        :param user_service_types: use to define own set of services and their coefficient --> dict (default None)
                with service types as keys and coefficients as values
        :param city: city to chose data and projection --> str

        :return: dict containing FeatureCollections of houses, services,
                service_types (only when object_type is house) and isochrone (only when object_type is service)
        """

        city_inf_model = self.cities_inf_model["Saint_Petersburg"]
        # Get service coefficient from DB or user request
        service_coef = self.parse_service_coefficients(user_service_types, living_situation_id)
        if type(service_coef) is tuple:
            return service_coef

        objects = BCAM.get_provision_info(object_type, functional_object_id,
                                          list(service_coef["service_code"]), provision_type)
        if type(objects) is tuple:
            return objects
        
        houses = gpd.GeoDataFrame.from_features(objects["houses"]).fillna(-1).set_crs(4326)
        services = gpd.GeoDataFrame.from_features(objects["services"]).fillna(-1).set_crs(4326)

        provision_columns = houses.filter(regex="provision").replace("None", np.nan)
        set_demand_columns = list(houses.filter(regex="demand").columns)
        set_num_service_columns = list(houses.filter(regex="available").columns)
        unprovided_columns = list(houses.filter(regex="unprovided").columns)

        available_service_type = [t.split("_provision")[0] for t in provision_columns.columns]
        service_coef = service_coef[service_coef["service_code"].isin(available_service_type)].sort_values(
            "service_code")
        wellbeing = self.calculate_wellbeing(provision_columns, service_coef)

        if object_type == "house":
            provision_columns = provision_columns.reindex(sorted(provision_columns.columns), axis=1)
            weighted_provision_columns = provision_columns * service_coef.set_axis(provision_columns.columns)[
                "evaluation"]
            houses["mean_provision"] = weighted_provision_columns.apply(
                lambda x: x.mean() if len(x[x.notna()]) > 0 else None, axis=1)
            houses = houses.drop(set_demand_columns + set_num_service_columns + unprovided_columns, axis=1).join(
                wellbeing)
            service_types_info = self.calculate_wellbeing(provision_columns.iloc[0], service_coef, get_provision=True)

        elif object_type == "service":
            service_type = services.iloc[0]["city_service_type"]
            service_code = city_inf_model.get_service_code(service_type)
            drop_col = [col for col in set_demand_columns if service_code not in col] + \
                       [col for col in set_num_service_columns if service_code not in col]
            houses = houses.drop(drop_col + unprovided_columns, axis=1).join(wellbeing)
            isochrone = gpd.GeoDataFrame.from_features(objects["isochrone"]).set_crs(4326)

        outcome_dict = {"houses": eval(houses.reset_index(drop=True).fillna("None").to_crs(4326).to_json()),
                        "services": eval(services.reset_index(drop=True).fillna("None").to_crs(4326).to_json())}

        if "service_types_info" in locals():
            outcome_dict["service_types"] = eval(service_types_info.to_json())
        elif "isochrone" in locals():
            outcome_dict["isochrone"] = eval(isochrone.to_json())
        return outcome_dict

    def get_wellbeing_aggregated(self, BCAM, area_type, living_situation_id=None, user_service_types=None,
                                 provision_type="calculated", city="Saint_Petersburg"):

        city_inf_model, city_crs = self.cities_inf_model[city], self.cities_crs[city]
        block = city_inf_model.Base_Layer_Blocks.copy().to_crs(city_crs)
        mo = city_inf_model.Base_Layer_Municipalities.copy().to_crs(city_crs)
        district = city_inf_model.Base_Layer_Districts.copy().to_crs(city_crs)

        wellbeing = self.get_wellbeing(BCAM=BCAM, living_situation_id=living_situation_id, return_dfs=True,
                                       user_service_types=user_service_types, provision_type=provision_type)
        houses = wellbeing["houses"]
        houses_mean_provision = houses.groupby([f"{area_type}_id"]).mean().filter(regex="provision")
        houses_mean_wellbeing = houses.groupby([f"{area_type}_id"]).mean().filter(regex="wellbeing")
        houses_mean_stat = pd.concat([houses_mean_provision, houses_mean_wellbeing], axis=1)
        units = eval(area_type).set_index("id").drop(["center"], axis=1).join(houses_mean_stat)
        return json.loads(units.reset_index().fillna("None").to_crs(4326).to_json())

    def calculate_wellbeing(self, loc, coef_df, get_provision=False):

        if get_provision:
            provision = loc.sort_index()
            provision.index = [idx.split("_provision_")[0] for idx in provision.index]
            available_type = provision.notna()
            provision = provision[available_type]
            coef_df = coef_df.sort_values(by="service_code").set_index("service_code")["evaluation"][available_type]
            coef = list(coef_df)
            provision = list(provision)
            weighted_provision = [1 + 2 * coef[i] * (-1 + provision[i]) if coef[i] <= 0.5
                                  else provision[i] ** (8 * coef[i] - 3) for i in range(len(provision))]
            result = pd.DataFrame({"service_code": list(coef_df.index), "provision": provision,
                                   "coefficient": coef, "wellbeing": weighted_provision}).round(2)
            return result

        else:
            provision = loc.reindex(sorted(loc.columns), axis=1).to_numpy()
            coef_df = coef_df.sort_values(by="service_code").set_index("service_code")["evaluation"]
            coef = list(coef_df)
            weighted_provision = [list(1 + 2 * coef[i] * (-1 + provision[:, i])) if coef[i] <= 0.5
                                  else list(provision[:, i] ** (8 * coef[i] - 3)) for i in range(len(coef))]
            weighted_provision = np.array(weighted_provision).T
            general_wellbeing = np.nansum(weighted_provision * coef / sum(coef), axis=1)
            weighted_provision = np.c_[weighted_provision, general_wellbeing]
            weighted_index = [t + "_wellbeing" for t in coef_df.index] + ["wellbeing"]
            weighted_series = pd.DataFrame(weighted_provision, columns=weighted_index, index=loc.index).round(2)
            return weighted_series

    def parse_service_coefficients(self, user_service_types=None, living_situation_id=None):
        """
        :param user_service_types: use to define own set of services and their coefficient --> dict (default None)
                with service types as keys and coefficients as values
        :param living_situation_id: living situation id from DB --> int (default None)
        :return: DataFrame object containing columns with service types and coefficients --> DataFrame
        """
        city_inf_model = self.cities_inf_model["Saint_Petersburg"]
        if user_service_types and type(user_service_types) is dict:
            service_coef = pd.DataFrame([[key, user_service_types[key]] for key in user_service_types],
                                        columns=["service_code", "evaluation"])

        elif living_situation_id and (type(living_situation_id) is int or type(living_situation_id) is str):
            service_coef = city_inf_model.get_living_situation_evaluation(living_situation_id)
            if len(service_coef) == 0:
                return None, "Living situation id absents in DB"
        else:
            return None, "Invalid data to calculate well-being. Specify living situation or service types"

        # Because provision for house as service is not calculated
        if "houses" in service_coef["service_code"]:
            service_coef = service_coef[service_coef["service_code"] != "houses"]

        return service_coef






