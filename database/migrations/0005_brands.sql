-- Which of your brands (config/brands.yaml) a content item belongs to; NULL = default account.
ALTER TABLE content_items ADD COLUMN brand TEXT;
CREATE INDEX IF NOT EXISTS idx_content_brand ON content_items(brand, platform, state);
