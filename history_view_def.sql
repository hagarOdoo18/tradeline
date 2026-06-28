 WITH legacy_source_db AS (
         SELECT max((legacy_product_month_fact.source_db)::text) AS source_db
           FROM legacy_product_month_fact
        ), legacy_sales AS (
         SELECT lmf.period_month,
            'legacy'::text AS source_system,
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_default_code)::text)
                    END,
                    CASE
                        WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_barcode)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_default_code)::text)
                    END,
                    CASE
                        WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_barcode)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END AS bucket_key,
            min(COALESCE(NULLIF((lmf.source_name)::text, ''::text), NULLIF((lmf.source_default_code)::text, ''::text), NULLIF((lmf.source_barcode)::text, ''::text), '[No Name]'::text)) AS bucket_name,
            "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                CASE
                    WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((lmf.source_default_code)::text)
                END,
                CASE
                    WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((lmf.source_barcode)::text)
                END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5) AS bucket_code_prefix5,
            min(COALESCE(NULLIF((lmf.source_default_code)::text, ''::text), NULLIF((lmf.source_barcode)::text, ''::text), '[No Code]'::text)) AS sample_item_code,
            min((lmf.source_category_name)::text) AS source_category_name,
            min((lmf.source_brand_name)::text) AS source_brand_name,
            NULL::integer AS branch_id,
            NULL::integer AS team_id,
            NULL::integer AS invoice_user_id,
            NULL::integer AS company_id,
            count(DISTINCT lmf.source_product_id) AS product_count,
            sum(COALESCE(lmf.legacy_sales_qty, (0.0)::double precision)) AS sales_qty,
            sum(COALESCE(lmf.legacy_sales_amount, (0.0)::double precision)) AS sales_amount,
            sum(COALESCE(lmf.legacy_return_qty, (0.0)::double precision)) AS return_qty,
            sum(COALESCE(lmf.legacy_return_amount, (0.0)::double precision)) AS return_amount,
            sum(COALESCE(lmf.legacy_discount_amount, (0.0)::double precision)) AS discount_amount,
            sum(COALESCE(lmf.legacy_gross_sales_amount, (0.0)::double precision)) AS gross_sales_amount,
                CASE
                    WHEN (sum(COALESCE(lmf.legacy_sales_qty, (0.0)::double precision)) = (0)::double precision) THEN NULL::double precision
                    ELSE (sum(COALESCE(lmf.legacy_sales_amount, (0.0)::double precision)) / sum(COALESCE(lmf.legacy_sales_qty, (0.0)::double precision)))
                END AS asp,
                CASE
                    WHEN bool_or(COALESCE(lmf.legacy_cost_available, false)) THEN sum(COALESCE(lmf.legacy_cogs_amount, (0.0)::double precision))
                    ELSE NULL::double precision
                END AS cogs_amount,
                CASE
                    WHEN bool_or(COALESCE(lmf.legacy_cost_available, false)) THEN sum(COALESCE(lmf.legacy_margin_amount, (COALESCE(lmf.legacy_sales_amount, (0.0)::double precision) - COALESCE(lmf.legacy_cogs_amount, (0.0)::double precision))))
                    ELSE NULL::double precision
                END AS margin_amount,
                CASE
                    WHEN (bool_or(COALESCE(lmf.legacy_cost_available, false)) AND (sum(COALESCE(lmf.legacy_sales_amount, (0.0)::double precision)) <> (0)::double precision)) THEN ((sum(COALESCE(lmf.legacy_margin_amount, (COALESCE(lmf.legacy_sales_amount, (0.0)::double precision) - COALESCE(lmf.legacy_cogs_amount, (0.0)::double precision)))) / sum(COALESCE(lmf.legacy_sales_amount, (0.0)::double precision))) * (100.0)::double precision)
                    ELSE NULL::double precision
                END AS margin_pct,
            bool_or(COALESCE(lmf.legacy_cost_available, false)) AS cost_available
           FROM legacy_product_month_fact lmf
          WHERE (lmf.period_month < '2026-01-01'::date)
          GROUP BY lmf.period_month,
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_default_code)::text)
                    END,
                    CASE
                        WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_barcode)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_default_code)::text)
                    END,
                    CASE
                        WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_barcode)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END, ("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                CASE
                    WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((lmf.source_default_code)::text)
                END,
                CASE
                    WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((lmf.source_barcode)::text)
                END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5))
         HAVING (
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_default_code)::text)
                    END,
                    CASE
                        WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_barcode)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (lmf.source_default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_default_code)::text)
                    END,
                    CASE
                        WHEN (lmf.source_barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(lmf.source_barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((lmf.source_barcode)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END IS NOT NULL)
        ), current_report_sales AS (
         SELECT (date_trunc('month'::text, (COALESCE(am.invoice_date, am.date))::timestamp with time zone))::date AS period_month,
            'current'::text AS source_system,
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END AS bucket_key,
            min(COALESCE(NULLIF((pt.name ->> 'en_US'::text), ''::text), NULLIF((pp.barcode)::text, ''::text), NULLIF((pp.default_code)::text, ''::text), '[No Name]'::text)) AS bucket_name,
            "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                CASE
                    WHEN (pp.barcode IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.barcode)::text)
                END,
                CASE
                    WHEN (pp.default_code IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.default_code)::text)
                END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5) AS bucket_code_prefix5,
            min(COALESCE(
                CASE
                    WHEN (pp.barcode IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.barcode)::text)
                END,
                CASE
                    WHEN (pp.default_code IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.default_code)::text)
                END)) AS sample_item_code,
            min((pc.complete_name)::text) AS source_category_name,
            NULL::text AS source_brand_name,
            am.branch_id,
            am.team_id,
            am.invoice_user_id,
            am.company_id,
            count(DISTINCT aml.product_id) AS product_count,
            sum(COALESCE(aml.signed_quantity, (0.0)::double precision)) AS sales_qty,
            sum((- COALESCE(aml.balance, 0.0))) AS sales_amount,
            sum(
                CASE
                    WHEN ((am.move_type)::text = 'out_refund'::text) THEN abs(COALESCE(aml.signed_quantity, (0.0)::double precision))
                    ELSE (0.0)::double precision
                END) AS return_qty,
            sum(
                CASE
                    WHEN ((am.move_type)::text = 'out_refund'::text) THEN abs((- COALESCE(aml.balance, 0.0)))
                    ELSE 0.0
                END) AS return_amount,
            sum(COALESCE((aml.total_cost)::double precision, (COALESCE(aml.standard_price, (0.0)::double precision) * COALESCE(aml.signed_quantity, (0.0)::double precision)))) AS cogs_amount,
            ((sum((- COALESCE(aml.balance, 0.0))))::double precision - sum(COALESCE((aml.total_cost)::double precision, (COALESCE(aml.standard_price, (0.0)::double precision) * COALESCE(aml.signed_quantity, (0.0)::double precision))))) AS margin_amount,
            bool_or(((aml.total_cost IS NOT NULL) OR (aml.standard_price IS NOT NULL))) AS cost_available
           FROM ((((account_move_line aml
             JOIN account_move am ON ((am.id = aml.move_id)))
             JOIN product_product pp ON ((pp.id = aml.product_id)))
             JOIN product_template pt ON ((pt.id = pp.product_tmpl_id)))
             LEFT JOIN product_category pc ON ((pc.id = pt.categ_id)))
          WHERE ((aml.product_id IS NOT NULL) AND ((COALESCE(aml.display_type, 'product'::character varying))::text = 'product'::text) AND ((am.state)::text = 'posted'::text) AND ((am.move_type)::text = ANY ((ARRAY['out_invoice'::character varying, 'out_refund'::character varying])::text[])) AND (COALESCE(am.invoice_date, am.date) >= '2026-01-01'::date))
          GROUP BY ((date_trunc('month'::text, (COALESCE(am.invoice_date, am.date))::timestamp with time zone))::date),
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END, ("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                CASE
                    WHEN (pp.barcode IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.barcode)::text)
                END,
                CASE
                    WHEN (pp.default_code IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.default_code)::text)
                END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)), am.branch_id, am.team_id, am.invoice_user_id, am.company_id
         HAVING (
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END IS NOT NULL)
        ), current_line_extras AS (
         SELECT (date_trunc('month'::text, (COALESCE(am.invoice_date, am.date))::timestamp with time zone))::date AS period_month,
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END AS bucket_key,
            "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                CASE
                    WHEN (pp.barcode IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.barcode)::text)
                END,
                CASE
                    WHEN (pp.default_code IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.default_code)::text)
                END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5) AS bucket_code_prefix5,
            NULL::integer AS branch_id,
            NULL::integer AS team_id,
            NULL::integer AS invoice_user_id,
            NULL::integer AS company_id,
            sum((((COALESCE(aml.price_unit, 0.0))::double precision * COALESCE(aml.signed_quantity, (0.0)::double precision)) * ((COALESCE(aml.discount, 0.0) / 100.0))::double precision)) AS discount_amount,
            sum(((COALESCE(aml.price_unit, 0.0))::double precision * COALESCE(aml.signed_quantity, (0.0)::double precision))) AS gross_sales_amount
           FROM ((account_move_line aml
             JOIN account_move am ON ((am.id = aml.move_id)))
             JOIN product_product pp ON ((pp.id = aml.product_id)))
          WHERE ((aml.product_id IS NOT NULL) AND ((COALESCE(aml.display_type, 'product'::character varying))::text = 'product'::text) AND ((am.state)::text = 'posted'::text) AND ((am.move_type)::text = ANY ((ARRAY['out_invoice'::character varying, 'out_refund'::character varying])::text[])) AND (COALESCE(am.invoice_date, am.date) >= '2026-01-01'::date))
          GROUP BY ((date_trunc('month'::text, (COALESCE(am.invoice_date, am.date))::timestamp with time zone))::date),
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END, ("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                CASE
                    WHEN (pp.barcode IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.barcode)::text)
                END,
                CASE
                    WHEN (pp.default_code IS NULL) THEN NULL::text
                    WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                    ELSE btrim((pp.default_code)::text)
                END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5))
         HAVING (
                CASE
                    WHEN (NULLIF("left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5), ''::text) IS NULL) THEN NULL::text
                    ELSE "left"(COALESCE(NULLIF(regexp_replace(upper(COALESCE(COALESCE(
                    CASE
                        WHEN (pp.barcode IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.barcode, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.barcode)::text)
                    END,
                    CASE
                        WHEN (pp.default_code IS NULL) THEN NULL::text
                        WHEN (lower(btrim((COALESCE(pp.default_code, ''::character varying))::text)) = ANY (ARRAY[''::text, 'false'::text, 'none'::text, 'null'::text])) THEN NULL::text
                        ELSE btrim((pp.default_code)::text)
                    END), ''::text)), '[^A-Z0-9]+'::text, ''::text, 'g'::text), ''::text), ''::text), 5)
                END IS NOT NULL)
        ), current_sales AS (
         SELECT COALESCE(crs.period_month, cle.period_month) AS period_month,
            'current'::text AS source_system,
            COALESCE(crs.bucket_key, cle.bucket_key) AS bucket_key,
            crs.bucket_name,
            COALESCE(crs.bucket_code_prefix5, cle.bucket_code_prefix5) AS bucket_code_prefix5,
            crs.sample_item_code,
            crs.source_category_name,
            crs.source_brand_name,
            COALESCE(crs.branch_id, cle.branch_id) AS branch_id,
            COALESCE(crs.team_id, cle.team_id) AS team_id,
            COALESCE(crs.invoice_user_id, cle.invoice_user_id) AS invoice_user_id,
            COALESCE(crs.company_id, cle.company_id) AS company_id,
            COALESCE(crs.product_count, (0)::bigint) AS product_count,
            COALESCE(crs.sales_qty, (0.0)::double precision) AS sales_qty,
            COALESCE(crs.sales_amount, 0.0) AS sales_amount,
            COALESCE(crs.return_qty, (0.0)::double precision) AS return_qty,
            COALESCE(crs.return_amount, 0.0) AS return_amount,
            COALESCE(cle.discount_amount, (0.0)::double precision) AS discount_amount,
            COALESCE(cle.gross_sales_amount, (0.0)::double precision) AS gross_sales_amount,
                CASE
                    WHEN (COALESCE(crs.sales_qty, (0.0)::double precision) = (0)::double precision) THEN NULL::double precision
                    ELSE ((COALESCE(crs.sales_amount, 0.0))::double precision / COALESCE(crs.sales_qty, (0.0)::double precision))
                END AS asp,
            crs.cogs_amount,
            crs.margin_amount,
                CASE
                    WHEN ((COALESCE(crs.sales_amount, 0.0) = (0)::numeric) OR (crs.margin_amount IS NULL)) THEN NULL::double precision
                    ELSE ((crs.margin_amount / (crs.sales_amount)::double precision) * (100.0)::double precision)
                END AS margin_pct,
            COALESCE(crs.cost_available, false) AS cost_available
           FROM (current_report_sales crs
             FULL JOIN current_line_extras cle ON (((cle.period_month = crs.period_month) AND (cle.bucket_key = crs.bucket_key) AND (COALESCE(cle.branch_id, 0) = COALESCE(crs.branch_id, 0)) AND (COALESCE(cle.team_id, 0) = COALESCE(crs.team_id, 0)) AND (COALESCE(cle.invoice_user_id, 0) = COALESCE(crs.invoice_user_id, 0)) AND (COALESCE(cle.company_id, 0) = COALESCE(crs.company_id, 0)))))
        ), combined AS (
         SELECT lsd.source_db,
            ls.period_month,
            ls.source_system,
            ls.bucket_name,
            ls.bucket_key,
            ls.bucket_code_prefix5,
            ls.sample_item_code,
            ls.source_category_name,
            ls.source_brand_name,
            ls.branch_id,
            ls.team_id,
            ls.invoice_user_id,
            ls.company_id,
            ls.product_count,
            ls.sales_qty,
            ls.sales_amount,
            ls.return_qty,
            ls.return_amount,
            ls.discount_amount,
            ls.gross_sales_amount,
            ls.asp,
            ls.cogs_amount,
            ls.margin_amount,
            ls.margin_pct,
            COALESCE(ls.cost_available, false) AS cost_available
           FROM (legacy_sales ls
             CROSS JOIN legacy_source_db lsd)
        UNION ALL
         SELECT lsd.source_db,
            cs.period_month,
            cs.source_system,
            cs.bucket_name,
            cs.bucket_key,
            cs.bucket_code_prefix5,
            cs.sample_item_code,
            cs.source_category_name,
            cs.source_brand_name,
            cs.branch_id,
            cs.team_id,
            cs.invoice_user_id,
            cs.company_id,
            cs.product_count,
            cs.sales_qty,
            cs.sales_amount,
            cs.return_qty,
            cs.return_amount,
            cs.discount_amount,
            cs.gross_sales_amount,
            cs.asp,
            cs.cogs_amount,
            cs.margin_amount,
            cs.margin_pct,
            COALESCE(cs.cost_available, false) AS cost_available
           FROM (current_sales cs
             CROSS JOIN legacy_source_db lsd)
        )
 SELECT row_number() OVER (ORDER BY period_month, source_system, bucket_code_prefix5, bucket_name, sample_item_code) AS id,
    source_db,
    period_month,
    source_system,
    bucket_name,
    bucket_key,
    bucket_code_prefix5,
    sample_item_code,
    source_category_name,
    source_brand_name,
    branch_id,
    team_id,
    invoice_user_id,
    company_id,
    product_count,
    sales_qty,
    sales_amount,
    return_qty,
    return_amount,
    discount_amount,
    gross_sales_amount,
    asp,
    cogs_amount,
    margin_amount,
    margin_pct,
    cost_available,
    cost_available AS margin_comparable
   FROM combined
  WHERE (bucket_key IS NOT NULL);