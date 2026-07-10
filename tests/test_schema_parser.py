"""Tests for uce.ingestion.schema_parser — all dialect parsers."""
import pytest

from uce.ingestion.schema_parser import extract_tables, parse_schema_file


# ---------------------------------------------------------------------------
# SQL CREATE TABLE
# ---------------------------------------------------------------------------

def test_sql_create_table_basic():
    schema = """
    CREATE TABLE users (
        id SERIAL PRIMARY KEY,
        name VARCHAR(100) NOT NULL,
        email TEXT UNIQUE
    );
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "users" in names
    user_table = next(t for t in tables if t["name"].lower() == "users")
    assert "id" in user_table["columns"]
    assert "name" in user_table["columns"]
    assert "email" in user_table["columns"]


def test_sql_multiple_tables():
    schema = """
    CREATE TABLE orders (id INT, user_id INT, total DECIMAL);
    CREATE TABLE products (id INT, name TEXT, price DECIMAL);
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "orders" in names
    assert "products" in names


# ---------------------------------------------------------------------------
# Drizzle pgTable
# ---------------------------------------------------------------------------

def test_pgtable_basic():
    schema = """
    export const users = pgTable('users', {
        id: serial('id').primaryKey(),
        username: text('username').notNull(),
        createdAt: timestamp('created_at'),
    });
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "users" in names


# ---------------------------------------------------------------------------
# Prisma
# ---------------------------------------------------------------------------

def test_prisma_model_basic():
    schema = """
    model User {
      id    Int    @id @default(autoincrement())
      name  String
      email String @unique
      posts Post[]
    }

    model Post {
      id      Int    @id
      title   String
      content String?
      authorId Int
    }
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "user" in names
    assert "post" in names

    user = next(t for t in tables if t["name"].lower() == "user")
    assert "id" in user["columns"]
    assert "name" in user["columns"]
    assert "email" in user["columns"]


def test_prisma_skips_directives():
    schema = """
    model Config {
      id    Int @id
      value String
      @@unique([id, value])
    }
    """
    tables = extract_tables(schema)
    config_table = next((t for t in tables if t["name"].lower() == "config"), None)
    assert config_table is not None
    # @@ directives should not appear as columns
    for col in config_table["columns"]:
        assert not col.startswith("@")


# ---------------------------------------------------------------------------
# SQLAlchemy declarative
# ---------------------------------------------------------------------------

def test_sqlalchemy_declarative():
    schema = """
from sqlalchemy import Column, Integer, String
from sqlalchemy.ext.declarative import declarative_base

Base = declarative_base()

class User(Base):
    __tablename__ = 'users'
    id = Column(Integer, primary_key=True)
    name = Column(String(100))
    email = Column(String(255), unique=True)
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "users" in names
    user = next(t for t in tables if t["name"].lower() == "users")
    assert "id" in user["columns"]
    assert "name" in user["columns"]
    assert "email" in user["columns"]


def test_sqlalchemy_core_table():
    schema = """
from sqlalchemy import Table, Column, Integer, String, MetaData

metadata = MetaData()
accounts = Table('accounts', metadata,
    Column('id', Integer, primary_key=True),
    Column('username', String),
    Column('balance', Integer),
)
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "accounts" in names
    acct = next(t for t in tables if t["name"].lower() == "accounts")
    assert "id" in acct["columns"]
    assert "username" in acct["columns"]
    assert "balance" in acct["columns"]


# ---------------------------------------------------------------------------
# Django
# ---------------------------------------------------------------------------

def test_django_model():
    schema = """
from django.db import models

class Article(models.Model):
    title = models.CharField(max_length=200)
    body = models.TextField()
    published = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = 'articles'
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "articles" in names
    art = next(t for t in tables if t["name"].lower() == "articles")
    assert "title" in art["columns"]
    assert "body" in art["columns"]
    assert "published" in art["columns"]


def test_django_model_default_table_name():
    schema = """
class Order(models.Model):
    total = models.DecimalField(max_digits=10, decimal_places=2)
    status = models.CharField(max_length=50)
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    # Default is lowercased class name
    assert "order" in names


# ---------------------------------------------------------------------------
# TypeORM
# ---------------------------------------------------------------------------

def test_typeorm_entity():
    schema = """
import { Entity, Column, PrimaryGeneratedColumn } from 'typeorm';

@Entity('products')
export class Product {
    @PrimaryGeneratedColumn()
    id: number;

    @Column()
    name: string;

    @Column({ unique: true })
    sku: string;
}
    """
    tables = extract_tables(schema)
    names = {t["name"].lower() for t in tables}
    assert "products" in names
    prod = next(t for t in tables if t["name"].lower() == "products")
    assert "name" in prod["columns"]
    assert "sku" in prod["columns"]


# ---------------------------------------------------------------------------
# Empty input edge cases
# ---------------------------------------------------------------------------

def test_empty_schema():
    tables = extract_tables("")
    assert tables == []


def test_schema_no_tables():
    tables = extract_tables("-- just a comment\nSELECT 1;")
    assert tables == []


# ---------------------------------------------------------------------------
# parse_schema_file returns empty list for missing file
# ---------------------------------------------------------------------------

def test_parse_schema_file_missing():
    result = parse_schema_file("/nonexistent/path/schema.sql")
    assert result == []
