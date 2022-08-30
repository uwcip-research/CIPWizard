""" This module focuses on functions which pull tree-structured data, such as reply threads,
and subsequently modifies them.
"""


import pickle
import os
import pandas as pd
import lxml.etree as etree
import csv
import networkx as nx

from psycopg2 import sql
from pprint import pprint
from collections import defaultdict
from tqdm import tqdm
from datetime import date, timedelta, datetime, timezone

from cipwizard.core import sql_statements
from cipwizard.core.util import clean, c, get_last_modified, \
    within_time_bounds, open_database, close_database, \
    get_column_header_dict, to_list_of_dicts, save_to_csv, \
    sql_type_dictionary


def export_reply_threads(database_name, 
        db_config_file, 
        table_name,
        select_columns=['tweet', 'user_id', 'user_name',
         'user_screen_name', 'created_at', 
        'in_reply_to_user_id', 'in_reply_to_user_screen_name',
        'in_reply_to_status_id', 'user_followers_count'],
        seed_conditions=None,
        seed_db_config_file=None,
        seed_database_name=None,
        seed_table_name=None,
        seed_limit=500,
        seed_random_percent=None,
        reply_range=[1, 10000000000000],
        verbose=False,
        output_type='csv',
        output_filepath=None,
        replies_table=None,
        seed_table=None,
        output_table=None):

    """ If not seed database is provided, we assume that you are pulling your seed posts
    from the same database your are pulling replies from.
    """
    if seed_database_name is None:
        seed_database_name = database_name
    if seed_db_config_file is None:
        seed_db_config_file = db_config_file

    """ The cursor is a curious concept in SQL. All queries need to run through the cursor
    object, and you need to generate one with Python. I use a function in cipwizard.core.util
    to do this easily. You need a unique cursor for each server/database combination.
    """
    seed_database, seed_cursor = open_database(seed_database_name, seed_db_config_file)
    database, cursor = open_database(database_name, db_config_file)

    """ Step 1, get the seed posts! Check the function below this one for additional documentation.
    """
    if 'id' not in select_columns:
        select_columns = select_columns + ['id']
    seed_posts = get_seed_posts(seed_cursor, seed_limit, seed_conditions, select_columns,
            seed_random_percent, seed_table_name, verbose)

    """ A bit of magic is done here to make sure that 'id' and 'in_reply_to_status_id'
    are in select_columns, and makes sure that the 'id' column is the first one in
    select_columns. This is over-complicated, and should probably be revised later :).
    """
    if 'in_reply_to_status_id' not in select_columns:
        select_columns = select_columns + ['in_reply_to_status_id']
    select_columns.insert(0, select_columns.pop(select_columns.index('id')))

    """ Postgres hasn't been using indexes for me recently. This forces it to. TODO: Fix
        or parameterize.
    """
    sql_statement = sql.SQL("""
        SET enable_seqscan = OFF;
        """)
    cursor.execute(sql_statement)

    reply_index = 0
    header = select_columns + ['path', 'depth', 'is_seed', 'seed_id']

    """ Step 2, retrieve the replies and write them to a csv document.
    """
    if output_type == 'csv':
        with open(output_filepath, 'w') as openfile:

            writer = csv.writer(openfile, delimiter=',')
            writer.writerow(header)
     
            for seed_post in tqdm(seed_posts):

                """ First, write the row for your seed post. The last three columns are only applicable
                    for replies, so fill them with dummy values.
                """
                writer.writerow([seed_post[key] for key in select_columns] + ['NONE', 1, 'TRUE', seed_post['id']])

                """ Then, pull the replies! See the get_reply_thread function for more.
                """
                results = get_reply_thread(cursor, seed_post, table_name, select_columns,
                        reply_range, verbose)

                """ IF there are replies, this recursive function will write them to your csv-file in
                    'reply order.' In other words, replies will be threaded as they are in e.g. Reddit.
                """
                if results:
                    for idx, result in enumerate(results):
                        if result['depth'] == 2:
                            construct_tree(result, results, idx, writer, select_columns)

    elif output_type == 'networkx':

        """ This code segment is for pulling out data into networkx format. It will be documented later :)
        """

        with open(output_filepath, 'wb') as openfile:

            thread_networks = []

            for seed_post in tqdm(seed_posts):

                results = get_reply_thread(cursor, seed_post, table_name, select_columns,
                        reply_range)

                G = nx.Graph(id=seed_post['id'])
                G.add_node(seed_post['id'], time=seed_post['created_at'], user_id=seed_post['user_id'], depth=1)

                if results:
                    for idx, result in enumerate(results):
                        G.add_node(result['id'], time=result['created_at'], user_id=result['user_id'], depth=result['depth'])
                        G.add_edge(result['id'], result['in_reply_to_status_id'])

                thread_networks += [G]

            pickle.dump(thread_networks, openfile)

    elif output_type == 'database':

        select_columns_sql = sql_statements.select_cols(select_columns + ['depth', 'path', 'is_seed', 'seed_id', 'order_id'])

        for seed_post in tqdm(seed_posts):

            """ Then, pull the replies! See the get_reply_thread function for more.
            """
            results = get_reply_thread(cursor, seed_post, table_name, select_columns,
                    reply_range, verbose)

            """ Insert into table. Parameters are currently hard-coded.
            """
            header_value = [seed_post[key] for key in select_columns] + [1, str(seed_post['id']), 'TRUE', seed_post['id'], reply_index]
            sql_statement = sql.SQL("INSERT INTO {output_table} ({select_cols}) VALUES (" + ', '.join(['%s'] * len(header_value)) + ")"). \
                    format(select_cols=select_columns_sql,
                            output_table=sql.SQL(output_table))
            cursor.execute(sql_statement, tuple(header_value))

            reply_index += 1

            if results:

                for idx, result in enumerate(results):
                    if result['depth'] == 2:
                        write_results = construct_tree_nowrite(result, results, idx, [])

                        input_values = [list(result.values()) + ['FALSE'] for result in write_results]
                        for input_value in input_values:
                            sql_statement = sql.SQL("INSERT INTO {output_table} ({select_cols}) VALUES (" + ', '.join(['%s'] * len(header_value)) + ")"). \
                                format(select_cols=select_columns_sql, output_table=sql.SQL(output_table))
                            cursor.execute(sql_statement, tuple(input_value + [seed_post['id'], reply_index]))
                            reply_index += 1

            database.commit()

        return


def get_seed_posts(cursor, seed_limit, seed_conditions,
            select_columns, seed_random_percent, seed_table_name,
            verbose=False):

    """ Creates and executes a simple SQL statement to retrieve posts from
    a table subject to certain conditions. We take advantage of the 'format'
    function to insert variables into a SQL template defined below. Note that
    we have to use psycopg2's "sql.SQL" function to wrap our inserted variables,
    or else it will get mad at us. For more details on why they require this,
    see Paul's comments about SQL injection -- although I don't think I have
    entirely prevented SQL injection here.
    """

    seed_limit = sql_statements.limit(seed_limit) 
    select_columns_sql = sql_statements.select_cols(select_columns)

    sql_statement = sql.SQL("""
        SELECT {select_columns}
        FROM {table_name}
        {random}
        {seed_conditions}
        {seed_limit}
        """).format(table_name=sql.SQL(seed_table_name),
                random=sql_statements.random_sample(seed_random_percent),
                seed_limit=seed_limit,
                seed_conditions=seed_conditions,
                select_columns=select_columns_sql)

    # This will print the SQL statement as-computed, so you can test it separately.
    if verbose:
        print(sql_statement.as_string(cursor))

    cursor.execute(sql_statement)
    seed_posts = to_list_of_dicts(cursor)

    return seed_posts


def get_reply_thread(cursor, seed_post, table_name, select_columns,
            reply_range, verbose):

    select_columns_sql = sql_statements.select_cols(select_columns)
    select_columns_child = sql_statements.select_cols(['c.' + col for col in select_columns])

    """ For the SQL command below, we will need to insert data types for each of our databases
    tweet columns. This bit of code creates the requisite SQL statement for this purpose.
    """
    types = sql_type_dictionary()
    column_types = ''
    for col in select_columns:
        if col == 'id':
            continue
        else:
            column_types += 'NULL::{} AS {},'.format(types[col], col)
    column_types = sql.SQL(column_types)

    """ This SQL template is where the magic happens. This is a recursive query. You give it
        one seed post, which we insert manually aboe the UNION ALL command, and use that to
        pull all posts that reply to that seed post. Then, you take each reply that you've
        captured, and run the same query again for each of them, getting each of THEIR replies.
        You do this until your queries are no longer returning results, thus going down the entire
        tree of replies for an original seed post.
    """

    post_id = seed_post['id']
    sql_statement = sql.SQL("""
        WITH RECURSIVE recursive_tweets({select_columns}, depth, path) AS (
        SELECT {seed_post}::bigint AS id,
        {column_types}
        1::INT AS depth,
        {seed_post}::TEXT AS path
        UNION ALL
        SELECT {select_columns_child}, p.depth + 1 AS depth, (p.path || '->' || c.id::TEXT)
        FROM recursive_tweets AS p, {table_name} AS c WHERE c.in_reply_to_status_id = p.id
        )
        SELECT * FROM recursive_tweets AS n;
        """).format(table_name=sql.SQL(table_name), seed_post=sql.SQL(str(post_id)),
                select_columns=select_columns_sql, select_columns_child=select_columns_child,
                column_types=column_types)

    """ The template is hard to understand -- running with verbose=True will let you see the
    command with variables inserted.
    """
    if verbose:
        print(sql_statement.as_string(cursor))

    cursor.execute(sql_statement)
    results = to_list_of_dicts(cursor)

    """ The first result is the seed post, which we don't need here.
    """
    results.pop(0)

    """ If you wanted to exclude posts with few (or too many) replies,
    you can use the reply_range variable here.
    """
    results_length = len(results)
    if reply_range is not None:
        if reply_range[0] <= results_length <= reply_range[1]:
            pass
        else:
            return None

    return results


def construct_tree(row, results, idx, writer, select_columns):

    """ A recursive function (!!) that reorganizes depth-sorted lists of tweets
    into thread-order, so that replies are always as close to their parents as
    possible.
    """

    depth = row['depth']
    parent_id = row['in_reply_to_status_id']
    writer.writerow([row[key] for key in select_columns + ['path', 'depth']] + ['FALSE'])
    for sub_idx, result in enumerate(results[idx:]):
        if result['depth'] == depth and result['in_reply_to_status_id'] == parent_id:
            continue
        elif result['in_reply_to_status_id'] == row['id']:
            construct_tree(result, results, idx + sub_idx, writer, select_columns)

    return


def construct_tree_nowrite(row, results, idx, output_results):

    depth = row['depth']
    parent_id = row['in_reply_to_status_id']
    output_results += [row]
    for sub_idx, result in enumerate(results[idx:]):
        if result['depth'] == depth and result['in_reply_to_status_id'] == parent_id:
            continue
        elif result['in_reply_to_status_id'] == row['id']:
            output_results = construct_tree_nowrite(result, results, idx + sub_idx, output_results)

    return output_results


if __name__ == '__main__':

    pass