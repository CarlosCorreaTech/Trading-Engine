-- Core: marketing channel dimension.
--
-- This table is the single source of truth for how a referring domain maps to
-- a channel. It is deliberately SQL rather than Python config so the mapping
-- is visible to anyone reading the models.
--
-- The hierarchy is strict: referrer_domain rolls up to channel, and channel
-- rolls up to channel_group. Placement detail (search vs video vs social)
-- hangs off the domain as traffic_source and is intentionally not part of the
-- rollup, because google.com and youtube.com are different placements of the
-- same Google Ads channel. Making placement a level of the hierarchy would
-- split Google into two channels and break any CAC computed per channel.
--
-- The mapping is last-click and therefore a simplification. Two honest
-- limitations are encoded in has_spend_data rather than glossed over:
--
--   TikTok sends 2,396 orders but there is no TikTok spend file, so its CAC is
--   uncomputable. Reporting it as a channel with zero cost would make it look
--   infinitely efficient.
--
--   Direct and blank referrers account for ~7,100 orders (27%). Some of that
--   is genuinely organic and some is paid traffic that lost its referrer.
--   Assigning it to any paid channel would flatter that channel's CAC, so it
--   is held separately and the semantic layer reports CAC as a range.
--
-- Instagram is folded into Meta because the Meta Ads account buys placements
-- across both. YouTube is folded into Google on the same logic, since PMax
-- serves YouTube inventory. That is the weaker of the two calls: YouTube also
-- carries organic traffic no campaign paid for, so Google's site-attributed
-- CAC is, if anything, flattered. The semantic layer reports Google CAC both
-- with and without YouTube so the reader can see how much it matters.

CREATE OR REPLACE TABLE core.dim_channel AS
SELECT * FROM (
    VALUES
        ('facebook.com',  'Meta',    'Paid',         'Paid Social', TRUE,  'Meta Ads buys Facebook placements'),
        ('instagram.com', 'Meta',    'Paid',         'Paid Social', TRUE,  'Meta Ads buys Instagram placements from the same account'),
        ('google.com',    'Google',  'Paid',         'Paid Search', TRUE,  'Google Ads search and shopping'),
        ('youtube.com',   'Google',  'Paid',         'Paid Video',  TRUE,  'PMax serves YouTube, but organic YouTube traffic is mixed in'),
        ('tiktok.com',    'TikTok',  'Paid',         'Paid Social', FALSE, 'Traffic present but no spend file supplied, so CAC is uncomputable'),
        ('direct',        'Direct',  'Unattributed', 'Direct',      FALSE, 'Self-reported direct; may include paid traffic that lost its referrer'),
        (NULL,            'Unknown', 'Unattributed', 'Unknown',     FALSE, 'No referrer recorded')
) AS t(referrer_domain, channel, channel_group, traffic_source, has_spend_data, mapping_note);
