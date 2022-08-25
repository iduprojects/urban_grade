import geopandas as gpd
import networkx as nx
import requests
import pandas as pd
import os
import ast
import numpy as np

from typing import Union
import shapely
from shapely.geometry import shape
from shapely import wkt
from sqlalchemy import create_engine
from geopandas.geodataframe import GeoDataFrame
from pandas.core.frame import DataFrame
from networkx.classes.multidigraph import MultiDiGraph

import pickle

import datetime

# TODO: SQL queries as a separate class
class DataQueryInterface:

    def __init__(self, city_name, cities_crs, cities_db_id):

        self.city_name = city_name
        self.city_crs = cities_crs[city_name]
        self.city_id = cities_db_id[city_name]

        self.mongo_address = "http://" + os.environ["MONGO"]
        self.engine = create_engine("postgresql://" + os.environ["POSTGRES"])
        
        # Select with city_id
        place_slice = {"place": "city", "place_id": self.city_id}
        
        # Graphs
        self.mobility_graph = self.get_graph_for_city(city_name, "intermodal_graph", node_type=int)
        self.mobility_graph = pickle.dumps(self.walk_graph)
        print(self.city_name, datetime.datetime.now(),'intermodal_graph')

        # Buildings
        buildings_columns = ["building_id as id", "building_area as basement_area", "is_living", "living_area", 
                            "population_balanced as population", "storeys_count", "administrative_unit_id", 
                            "municipality_id", "block_id", "geometry"]
        self.Buildings = self.get_buildings(buildings_columns, place_slice).to_crs(self.city_crs)
        self.Buildings = self.Buildings[
            (self.Buildings.geom_type == "MultiPolygon") | (self.Buildings.geom_type == "Polygon")
            ]
        self.Buildings = pickle.dumps(self.Buildings)
        print(self.city_name, datetime.datetime.now(),'Buildings')

        self.Spacematrix_Buildings = self.get_file_from_mongo("infrastructure", "spacematrix_buildings", "geojson")
        self.Spacematrix_Buildings = pickle.dumps(self.Spacematrix_Buildings)
        print(self.city_name, datetime.datetime.now(),'Spacematrix_Buildings')

        # Services
        service_columns = ["building_id", "functional_object_id as id", "city_service_type", "center",
                            "city_service_type_id", "city_service_type_code as service_code", "service_name",
                            "block_id", "administrative_unit_id", "municipality_id"]
        self.Services = self.get_services(service_columns, add_normative=True, place_slice=place_slice)
        print(self.city_name, datetime.datetime.now(),'Services')
        self.Public_Transport_Stops = self.Services[self.Services["service_code"] == "stops"]
        print(self.city_name, datetime.datetime.now(),'Public_Transport_Stops')
        self.Services = pickle.dumps(self.Services)
        self.Public_Transport_Stops = pickle.dumps(self.Public_Transport_Stops)

        # Blocks
        self.Spacematrix_Blocks = self.get_file_from_mongo("infrastructure", "Spacematrix_Blocks", "geojson")
        print(self.city_name, datetime.datetime.now(),'Spacematrix_Blocks')
        self.Block_Diversity = self.get_file_from_mongo("infrastructure", "Blocks_Diversity", "geojson")
        print(self.city_name, datetime.datetime.now(),'Block_Diversity')
        blocks_column = ["id", "municipality_id", "administrative_unit_id", "geometry"]
        self.Blocks = self.get_territorial_units("blocks", blocks_column, place_slice=place_slice)
        # temorary condition. area column must be recieved from db
        self.Blocks["area"] = None
        print(self.city_name, datetime.datetime.now(),'Blocks')
        self.Spacematrix_Blocks = pickle.dumps(self.Spacematrix_Blocks)
        self.Block_Diversity = pickle.dumps(self.Block_Diversity)
        self.Blocks = pickle.dumps(self.Blocks)

        # Municipalities
        self.Municipalities = self.get_territorial_units("municipalities", ["id", "geometry"], place_slice=place_slice)
        self.Municipalities = pickle.dumps(self.Municipalities)
        print(self.city_name, datetime.datetime.now(),'Municipality')

        # Districts
        self.Districts = self.get_territorial_units("administrative_units", ["id", "geometry"], place_slice=place_slice)
        self.Districts = pickle.dumps(self.Districts)
        print(self.city_name, datetime.datetime.now(),'Districts')
    
        # Provision
        if self.city_name == "Saint_Petersburg":
            print(self.city_name, datetime.datetime.now(), 'houses_provision')
            houses_provision = pd.read_sql_table("new_houses_provision_tmp", con=self.engine, schema="provision")
            houses_provision = self.transform_provision_file(houses_provision)
            self.houses_provision = houses_provision.rename(
                columns={"administrative_unit_id": "district_id", "municipality_id": "mo_id"})

            print(self.city_name, datetime.datetime.now(),'services_provision')
            services_provision = pd.read_sql("""
                    SELECT p.*, s.administrative_unit_id, s.municipality_id, s.block_id
                    FROM provision.new_services_load_tmp p
                    LEFT JOIN all_services s ON p.functional_object_id=s.functional_object_id""", con=self.engine)
            services_provision = self.transform_provision_file(services_provision)
            self.services_provision = services_provision.rename(
                columns={"administrative_unit_id": "district_id", "municipality_id": "mo_id"})
            chunk_size = 1000
            self.houses_provision = [pickle.dumps(self.houses_provision.iloc[i:i+chunk_size]) for i in range(0, len(self.houses_provision), chunk_size)]
            self.services_provision = [pickle.dumps(self.services_provision.iloc[i:i+chunk_size]) for i in range(0, len(self.services_provision), chunk_size)]
            del houses_provision, services_provision
        else:
            self.houses_provision = pickle.dumps(None)
            self.services_provision = None
        
        print(f"{city_name} is loaded")

    def get_graph_for_city(self, city: str, graph_type: str, node_type: type) -> MultiDiGraph:

        file_name = graph_type + "_" + city.lower()
        graph = requests.get(self.mongo_address + "/uploads/city_graphs/" + file_name)
        graph = nx.readwrite.graphml.parse_graphml(graph.text, node_type=node_type)
        if graph_type == "walk_graph" or graph_type == "drive_graph":
            graph = self.load_graph_geometry(graph)
        return graph

    @staticmethod
    def load_graph_geometry(graph: MultiDiGraph) -> MultiDiGraph:

        for u, v, data in graph.edges(data=True):
            data["geometry"] = wkt.loads(data["geometry"])

        return graph

    @staticmethod
    def set_xy_attributes(graph: MultiDiGraph) -> None:
        attrs = {}
        for i in list(graph.nodes()):
            attrs[i] = {"x": eval(i)[0], "y": eval(i)[1]}
        nx.set_node_attributes(graph, attrs)

    def generate_general_sql_query(self, table: str, columns: list, join_tables: str = None, equal_slice: dict = None,
                                   place_slice: dict = None) -> str:

        columns = ", ".join(columns)
        columns = columns.replace("t.geometry", "ST_AsGeoJSON(t.geometry) AS geometry")
        columns = columns.replace("t.center", "ST_AsGeoJSON(t.center) AS geometry")
        sql_query = f"""SELECT {columns} FROM {table} t """

        sql_query += join_tables if join_tables else ""
        where_statment = ""
        if equal_slice is not None:
            where_statment += f"WHERE {equal_slice['column']} = '{equal_slice['value']}' "
        if place_slice is not None:
            where_statment += "WHERE " if "WHERE" not in where_statment else "and "
            where_statment += self.get_place_slice(place_slice)
        sql_query += where_statment

        return sql_query

    @staticmethod
    def get_place_slice(conditions):

        if conditions["place"] == "polygon":
            slice_row = f"ST_intersects(b.geometry, ST_GeomFromText('POLYGON({polygon}), 4326')) = True"
        elif conditions["place"] == "municipality":
            slice_row = f"municipality_id = {conditions['place_id']}"
        elif conditions["place"] == "district":
            slice_row = f"administrative_unit_id={conditions['place_id']}"
        elif conditions["place"] == "city":
            slice_row = f"city_id = {conditions['place_id']}"
        else:
            raise ValueError("Incorrect area type.")

        return slice_row

    def get_territorial_units(self, territory_type: str, columns: list, place_slice: dict = None
                            ) -> Union[GeoDataFrame, DataFrame]:

        columns = ["t." + c for c in columns]
        sql_query = self.generate_general_sql_query(territory_type, columns, place_slice=place_slice)
        df = pd.read_sql(sql_query, con=self.engine)
        if "geometry" in df.columns:
            df['geometry'] = df['geometry'].apply(lambda x: shape(ast.literal_eval(x)))
            gdf = gpd.GeoDataFrame(df, geometry=df.geometry).set_crs(4326).to_crs(self.city_crs)
            return gdf
        else:
            return df

    def get_buildings(self, columns: list, place_slice: dict = None) -> Union[DataFrame, GeoDataFrame]:

        columns = ["t." + c for c in columns]
        sql_query = self.generate_general_sql_query("all_buildings", columns, place_slice=place_slice)
        df = pd.read_sql(sql_query, con=self.engine)
        if "geometry" in df.columns:
            df['geometry'] = df['geometry'].apply(lambda x: shape(ast.literal_eval(x)))
            gdf = gpd.GeoDataFrame(df, geometry=df.geometry).set_crs(4326).to_crs(self.city_crs)
            return gdf
        else:
            return df

    def get_services(self, columns: list, equal_slice: dict = None, place_slice: dict = None,
                     add_normative: bool = False) -> Union[GeoDataFrame, DataFrame]:

        join_table = ""
        columns = ["t." + c for c in columns]
        if add_normative:
            columns.extend(['s.houses_in_radius', 's.people_in_radius', 's.service_load',
                            's.needed_capacity AS loaded_capacity', 's.reserve_resource', 'n.normative'])
            join_table = f"""
            LEFT JOIN provision.services s ON functional_object_id = s.service_id 
            LEFT JOIN provision.normatives n ON functional_object_id = n.normative """

        sql_query = self.generate_general_sql_query(
            "all_services", columns, join_tables=join_table, equal_slice=equal_slice, place_slice=place_slice)
        sql_query = sql_query.replace("functional_object_id,", "functional_object_id AS index,")
        df = pd.read_sql(sql_query, con=self.engine)
        if "geometry" in df.columns:
            df['geometry'] = df['geometry'].apply(lambda x: shape(ast.literal_eval(x)))
            gdf = gpd.GeoDataFrame(df, geometry=df.geometry).set_crs(4326).to_crs(self.city_crs)
            return gdf
        else:
            return df

    def transform_provision_file(self, table):
        table = gpd.GeoDataFrame(table, geometry = table['geometry'].apply(lambda x: shapely.wkt.loads(x)), crs=4326).to_crs(self.city_crs)
        table = table.apply(lambda col: col.apply(lambda row: self.get_eval_values(row)))
        table = table.set_index("functional_object_id").replace("NaN", np.nan)
        return table

    @staticmethod
    def get_eval_values(value):
        try:
            parsed_value = eval(value)
            if type(parsed_value) in [str, int, float, dict, list]:
                return parsed_value
            else:
                return value
        except:
            return value

    # Temporary function
    def get_file_from_mongo(self, collection_name: str, file_name: str, file_type: str) \
            -> Union[GeoDataFrame, dict, None]:

        file_name = file_name + "_" + self.city_name.lower()
        response = requests.get(self.mongo_address + f"/uploads/{collection_name}/{file_name}")
        if file_type == "geojson":
            response = gpd.GeoDataFrame.from_features(response.json()).set_crs(4326).to_crs(self.city_crs) \
                if response.status_code == 200 else None
        elif file_type == "json":
            response = response.json() if response.status_code == 200 else None
        else:
            raise ValueError("Receiving this file format is not supported.")

        return response

