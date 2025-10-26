import sqlite3
import json

def setup_database():
    conn = sqlite3.connect('bot.db')
    c = conn.cursor()

    # Create tables
    c.execute('''
        CREATE TABLE IF NOT EXISTS objects (
            id INTEGER PRIMARY KEY,
            obj_id TEXT,
            obj_type TEXT,
            obj_name TEXT,
            obj_year INTEGER,
            obj_description TEXT,
            obj_url TEXT,
            obj_image TEXT,
            admin_rating REAL,
            site_rating REAL
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS genres (
            id INTEGER PRIMARY KEY,
            name TEXT UNIQUE
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS object_genres (
            object_id INTEGER,
            genre_id INTEGER,
            FOREIGN KEY(object_id) REFERENCES objects(id),
            FOREIGN KEY(genre_id) REFERENCES genres(id)
        )
    ''')

    c.execute('''
        CREATE TABLE IF NOT EXISTS user_views (
            user_id INTEGER,
            object_id INTEGER,
            PRIMARY KEY(user_id, object_id),
            FOREIGN KEY(object_id) REFERENCES objects(id)
        )
    ''')


if __name__ == '__main__':
    setup_database()
