#!/usr/bin/env python
#-*- coding:utf-8 -*-

"""
    *.py: Description of what * does.
    Last Modified:
"""

#import ipdb
import unicodecsv
import sqlite3
#import gsqlite3 as sqlite3
import sys
import os
# from time import sleep
from . import GeoPoint, safe_convert, isempty
from pymongo import MongoClient
import json
from pyelasticsearch import bulk_chunks, ElasticSearch
import gzip

unicodecsv.field_size_limit(sys.maxsize)

__author__ = "Sathappan Muthiah"
__email__ = "sathap1@vt.edu"
__version__ = "0.0.1"


sqltypemap = {'char': unicode,
              'BIGINT': int,
              'text': unicode, 'INT': int,
              'FLOAT': float, 'varchar': unicode
              }


class BaseDB(object):
    def __init__(self, dbpath):
        pass

    def insert(self, keys, values, **args):
        pass


class SQLiteWrapper(BaseDB):
    def __init__(self, dbpath, dbname="WorldGazetteer"):
        self.dbpath = dbpath
        self.conn = sqlite3.connect(dbpath, check_same_thread=False)
        self.conn.execute('PRAGMA synchronous = OFF')
        self.conn.execute('PRAGMA journal_mode = OFF')
        self.conn.execute("PRAGMA cache_size=5000000")
        self.conn.row_factory = sqlite3.Row
        self.cursor = self.conn.cursor()
        self.name = dbname

    def query(self, stmt=None, params=None):
        # self.conn = sqlite3.connect(self.dbpath)
        cursor = self.conn.cursor()
        cursor.row_factory = sqlite3.Row
        # ipdb.set_trace()
        # result = self.cursor.execute(stmt, params)
        result = cursor.execute(stmt, params)
        result = [GeoPoint(**dict(l)) for l in result.fetchall()]
        # result = [GeoPoint(**dict(l)) for l in result.fetchmany(1000)]
        # conn.close()
        return result

    def insert(self, msg, dup_id):
        return

    # outdated function
    def _create(self, csvPath, columns, delimiter="\t", coding='utf-8', **kwargs):
        with open(csvPath, "rU") as infile:
            infile.seek(0)
            reader = unicodecsv.DictReader(infile, dialect='excel',
                                           fieldnames=columns, encoding=coding)
            items = [r for r in reader]
            self.cursor.execute('''CREATE TABLE WorldGazetteer (id float,
                                    name text, alt_names text, orig_names text, type text,
                                    pop integer, latitude float, longitude float, country text,
                                    admin1 text, admin2 text, admin3 text)''')
            self.cursor.executemany('''INSERT INTO WorldGazetteer
                                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                                    [tuple([i[c] for c in columns]) for i in items])
            self.conn.commit()

    def create(self, csvfile, fmode='r', delimiter='\t',
               coding='utf-8', columns=[], header=False, **kwargs):
        with open(csvfile, fmode) as infile:
            infile.seek(0)
            if header:
                try:
                    splits = infile.next().split(delimiter)
                    if not columns:
                        columns = splits
                except Exception:
                    pass

            self.cursor.execute(''' CREATE TABLE IF NOT EXISTS {} ({})'''.format(self.name,
                                                                                 ','.join(columns)))

            reader = infile
            columnstr = ','.join(['?' for c in columns])

            self.cursor.executemany('''INSERT INTO {}
                                    VALUES ({})'''.format(self.name, columnstr),
                                    (tuple([i for i in c.decode("utf-8").split("\t")])
                                     for c in reader)
                                    )
            # (tuple([c[i] for i in columns]) for c in reader))
            self.conn.commit()
            if 'index' in kwargs:
                for i in kwargs['index']:
                    col, iname = i.split()
                    self.cursor.execute('''CREATE INDEX IF NOT
                                           EXISTS {} ON {} ({})'''.format(iname.strip(),
                                                                          self.name,
                                                                          col.strip()))
                    self.conn.commit()
            self.conn.commit()

    def close(self):
        self.conn.commit()

    def drop(self, tablename):
        self.cursor.execute("DROP TABLE IF EXISTS {}".format(tablename))
        self.conn.commit()


class DataReader(object):
    def __init__(self, dataFile, configFile):
        self.columns = self._readConfig(configFile)
        self.dataFile = gzip.open(dataFile)
        #self.dataFile = open(dataFile)

    def _readConfig(self, cfile):
        with open(cfile) as conf:
            cols = [l.split(" ", 1)[0] for l in json.load(conf)['columns']]

        return cols

    def next(self):
        row = dict(zip(self.columns, self.dataFile.next().decode("utf-8").lower().strip().split("\t")))
        return row

    def __iter__(self):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return self.close()

    def close(self):
        self.dataFile.close()


class ESWrapper(BaseDB):
    def __init__(self, index_name, host='http://localhost', port=9200):
        self.eserver = ElasticSearch(urls=host, port=port,
                                     timeout=60, max_retries=3)
        self._base_query = {"query": {"bool": {"must": {"match": {"name.raw": ""}}}}}
        self._geo_filter = {"geo_distance": {"distance": "20km", "coordinates": {}}}
        self._index = index_name
        self._doctype = "places"

    def query(self, qkey, qtype="exact"):
        """
        qtype values are exact, relaxed or geo_distance
        """
        q = self._base_query.copy()
        if qtype == "exact":
            q["query"]["bool"]["must"]["match"]["name.raw"] = qkey
        elif qtype == "relaxed":
            q["query"]["bool"]["must"]["match"]["name"] = qkey
            q["query"]["bool"]["must"]["match"].pop("name.raw")
        elif qtype == "geo_distance":
            q = {"query": {"bool": {"must": {"match_all": {}}},
                           "filter": {"geo_distance": {"distance": "20km",
                                                       "coordinates": qkey}}}}

        return self.eserver.search(q, index=self._index, doc_type=self._doctype)

    def near_geo(self, geo_point):
        q = {"query": {"bool": {"must": {"match_all": {}}},
                       "filter": self._geo_filter}}
        q["query"]["bool"]["geo_distance"]["coordinates"] = geo_point
        return self.eserver.search(q, index=self._index, doc_type=self._doctype)

    def create(self, datacsv, confDir="../data/"):
        with open(os.path.join(confDir, "es_settings.json")) as jf:
            settings = json.load(jf)

        self.eserver.create_index(index='geonames', settings=settings)
        for chunk in bulk_chunks(self._opLoader(datacsv, confDir),
                                 docs_per_chunk=1000):
            self.eserver.bulk(chunk, index='geonames', doc_type='places')
            print "..",

        self.eserver.refresh('geonames')

    def _opLoader(self, datacsv, confDir):
        with DataReader(datacsv, os.path.join(confDir, 'geonames.conf')) as reader:
            cnt = 0
            for row in reader:
                row['coordinates'] = [float(row['longitude']), float(row['latitude'])]
                del(row['latitude'])
                del(row['longitude'])
                row['alternatenames'] = row['alternatenames'].split(",")
                cnt += 1
                #if cnt > 100:
                    #break
                yield self.eserver.index_op(row, index="geonames", doc_type="places")


class MongoDBWrapper(BaseDB):
    def __init__(self, dbname, coll_name, host='localhost', port=12345):
        self.client = MongoClient(host, port)
        self.db = self.client[dbname]
        self.collection = self.db[coll_name]

    def query(self, stmt, items=[], limit=None):
        if limit:
            res = self.collection.find(stmt + {it: 1 for it in items}).limit(limit)
        else:
            res = self.collection.find(stmt + {it: 1 for it in items})

        return [GeoPoint(**d) for d in res]

    def create(self, cityCsv, admin1csv, admin2csv, countryCsv, confDir="../../data/"):
        with open(admin1csv) as acsv:
            with open(os.path.join(confDir, "geonames_admin1.conf")) as t:
                columns = [l.split(" ", 1)[0] for l in json.load(t)['columns']]

            admin1 = {}
            for l in acsv:
                row = dict(zip(columns, l.decode('utf-8').lower().split("\t")))
                admin1[row['key']] = row

        with open(countryCsv) as ccsv:
            with open(os.path.join(confDir, "geonames_countryInfo.conf")) as t:
                columns = [l.split(" ", 1)[0] for l in json.load(t)['columns']]

            countryInfo = {}
            for l in ccsv:
                row = dict(zip(columns, l.lower().decode('utf-8').split("\t")))
                countryInfo[row['ISO']] = row

        with open(admin2csv) as acsv:
            with open(os.path.join(confDir, "geonames_admin1.conf")) as t:
                columns = [l.split(" ", 1)[0] for l in json.load(t)['columns']]

            admin2 = {}
            for l in acsv:
                row = dict(zip(columns, l.decode('utf-8').lower().split("\t")))
                admin2[row['key']] = row

        with open(cityCsv) as ccsv:
            with open(os.path.join(confDir, "geonames.conf")) as t:
                conf = json.load(t)
                columns = [l.split(" ", 1)[0] for l in conf['columns']]
                coltypes = dict(zip(columns,
                                    [l.split(" ", 2)[1].split("(", 1)[0] for l in conf['columns']]))

            for l in ccsv:
                row = dict(zip(columns, l.decode("utf-8").lower().split("\t")))

                akey = row['countryCode'] + "." + row['admin1']
                if row['admin1'] == "00":
                    ad1_details = {}
                else:
                    try:
                        ad1_details = admin1[akey]
                    except:
                        print akey
                        ad1_details = {}

                if not isempty(row['admin2']):
                    try:
                        ad2_details = (admin2[akey + "." + row["admin2"]])
                    except:
                        ad2_details = {}
                else:
                    ad2_details = {}

                if row['countryCode'] != 'YU':
                    try:
                        country_name = countryInfo[row['countryCode']]
                    except:
                        country_name = {'country': None, 'ISO3': None, 'continent': None}
                else:
                    country_name = {'country': 'Yugoslavia', 'ISO3': "YUG", "continent": 'Europe'}

                for c in row:
                    row[c] = safe_convert(row[c], sqltypemap[coltypes[c]], None)

                if row['alternatenames']:
                    row['alternatenames'] = row['alternatenames'].split(",")

                row['country'] = country_name['country']
                row['continent'] = country_name['continent']
                row['countryCode_ISO3'] = country_name['ISO3']
                row['admin1'] = ad1_details.get('name', "")
                row['admin1_asciiname'] = ad1_details.get('asciiname', "")
                row['admin2'] = ad2_details.get('name', "")
                rkeys = row.keys()
                row['coordinates'] = [row['longitude'], row['latitude']]
                for c in rkeys:
                    if isempty(row[c]):
                        del(row[c])

                self.collection.insert(row)


def main(args):
    """
    """
    import json
    with open(args.config) as cfile:
        config = json.load(cfile)

    fmode = config['mode']
    separator = config['separator']
    columns = config['columns']
    coding = config['coding']
    dbname = args.name
    infile = args.file
    index = config['index']
    sql = SQLiteWrapper(dbpath="./GN_dump_20160407.sql", dbname=dbname)
    if args.overwrite:
        sql.drop(dbname)
    sql.create(infile, mode=fmode, columns=columns,
               delimiter=separator, coding=coding, index=index)

    sql.close()


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("-n", "--name", type=str, help="database name")
    ap.add_argument("-c", "--config", type=str, help="config file containing database info")
    ap.add_argument("-f", "--file", type=str, help="csv name")
    ap.add_argument("-o", "--overwrite", action="store_true",
                    default=False, help="flag to overwrite if already exists")
    args = ap.parse_args()
    main(args)
