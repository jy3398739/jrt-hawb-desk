-- HAWB 分单识别结果建表脚本（MySQL 8.0 / utf8mb4）
-- 两张表结构一致：hawb_raw 第一遍原文口径，hawb_air 第二遍航空口径
-- 注意 CONSIGNEE_INFO_CITTY 为双 T，沿用业务库表原拼写，勿改
-- *_INFO_EORI 是"识别号"唯一的格子（2026-10-10 业主定案）：两位国家字母开头的海关号(EORI/VAT)
-- 与票面税号（中国 USCI/统一社会信用代码、CNPJ、RFC、TAX ID、VAT NO）都落在这里，
-- 同一主体两个号用 " / " 拼在一格、两个都留，所以这一列放宽到 128。
-- 曾经有过的 *_INFO_TAX_ID 两列**已从建表语句删除**；老库里那两列**不要 DROP**（见文件末注释：
-- 留着不碍事，还保得住"当年确实解析出过这个号"的证据），只是新结果不再往里写。
-- SQL Server：将 VARCHAR->NVARCHAR、TEXT->NVARCHAR(MAX)、DOUBLE->FLOAT、
--             ENGINE/CHARSET 子句删除即可。

CREATE TABLE IF NOT EXISTS hawb_raw (
  source_file                 VARCHAR(255) NOT NULL PRIMARY KEY COMMENT '来源文件名(去扩展)',
  MAWB_NO                     VARCHAR(64),
  HAWB_NO                     VARCHAR(64),
  SHIPPER_INFO                TEXT,
  CONSIGNEE_INFO              TEXT,
  ORIGIN_NAME                 VARCHAR(128),
  TO1                         VARCHAR(8),
  TO2                         VARCHAR(8),
  TO3                         VARCHAR(8),
  DEST_NAME                   VARCHAR(128),
  GOODS_INFO                  TEXT,
  GOODS_HS_CODE               VARCHAR(64),
  PIECES                      INT,
  WEIGHT                      DOUBLE,
  SLAC                        INT,
  CREATE_TIME                 VARCHAR(32),
  SEND_STATUS                 VARCHAR(32),
  SHIPPER_INFO_COMP_NAME      VARCHAR(255),
  SHIPPER_INFO_COMP_ADDRESS   TEXT,
  SHIPPER_INFO_CITY           VARCHAR(128),
  SHIPPER_INFO_COUNTRY        VARCHAR(128),
  SHIPPER_INFO_STATE          VARCHAR(128),
  SHIPPER_INFO_POSTAL         VARCHAR(64),
  SHIPPER_INFO_TEL            VARCHAR(64),
  SHIPPER_INFO_FAX            VARCHAR(64),
  SHIPPER_INFO_EORI           VARCHAR(128),
  SHIPPER_INFO_AEO            VARCHAR(64),
  SHIPPER_INFO_EMAIL          VARCHAR(128),
  CONSIGNEE_INFO_COMP_NAME    VARCHAR(255),
  CONSIGNEE_INFO_COMP_ADDRESS TEXT,
  CONSIGNEE_INFO_CITTY        VARCHAR(128),
  CONSIGNEE_INFO_COUNTRY      VARCHAR(128),
  CONSIGNEE_INFO_STATE        VARCHAR(128),
  CONSIGNEE_INFO_POSTAL       VARCHAR(64),
  CONSIGNEE_INFO_TEL          VARCHAR(64),
  CONSIGNEE_INFO_FAX          VARCHAR(64),
  CONSIGNEE_INFO_EORI         VARCHAR(128),
  CONSIGNEE_INFO_AEO          VARCHAR(64),
  CONSIGNEE_INFO_EMAIL        VARCHAR(128),
  needs_review                TINYINT NOT NULL DEFAULT 0 COMMENT '1=有质检红旗(缺字段/L3码表缺项/保真未命中/处理失败)，需人工核票',
  review_flags                TEXT COMMENT '红旗清单，；分隔；NULL=该票无质检记录(改造前的旧结果)',
  updated_at                  TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='HAWB第一遍原文口径';

CREATE TABLE IF NOT EXISTS hawb_air LIKE hawb_raw;
ALTER TABLE hawb_air COMMENT='HAWB第二遍航空口径(ISO国家码/IATA城市码/电话归一化)';

-- === 老库升级（建表在 2026-09 之前、缺质检列时执行）===
-- 不升级的话 db_writer 会报 Unknown column 'needs_review'，失败清单里逐行可见。
-- ALTER TABLE hawb_raw ADD COLUMN needs_review TINYINT NOT NULL DEFAULT 0
--        COMMENT '1=有质检红旗，需人工核票';
-- ALTER TABLE hawb_raw ADD COLUMN review_flags TEXT
--        COMMENT '红旗清单，；分隔';
-- ALTER TABLE hawb_air ADD COLUMN needs_review TINYINT NOT NULL DEFAULT 0
--        COMMENT '1=有质检红旗，需人工核票';
-- ALTER TABLE hawb_air ADD COLUMN review_flags TEXT
--        COMMENT '红旗清单，；分隔';

-- === 老库升级（字段契约 37 -> 39：2026-09-21 之前建的表缺税号两列时执行）===
-- 这一档**已经作废**：2026-10-10 起 `*_INFO_TAX_ID` 两列从列面删除，本文件不再造它们，
-- 老库里已有的那两列**不要 DROP**（DROP 列不可逆，而留着既不碍事，也保得住"当年确实解析出过
-- 这个号"的证据；db_writer 的 INSERT 列表由 codes.TARGET_KEYS_OUT 推，不再提到它们）。
-- 只在"2026-09-21 之前建的表"才需要下面两行，为的是让老表能接住当时那份 39 列结果。
-- ALTER TABLE hawb_raw ADD COLUMN SHIPPER_INFO_TAX_ID VARCHAR(64),
--        ADD COLUMN CONSIGNEE_INFO_TAX_ID VARCHAR(64);
-- ALTER TABLE hawb_air ADD COLUMN SHIPPER_INFO_TAX_ID VARCHAR(64),
--        ADD COLUMN CONSIGNEE_INFO_TAX_ID VARCHAR(64);

-- === 老库升级（2026-10-10：识别号统一进 EORI，一格可能装两个号）===
-- 老表的 EORI 是 VARCHAR(64)：两个号拼起来一般 30-40 字符够用，但 MySQL 非严格模式放不下时
-- 是**静默截断**（号尾被切掉），所以放宽到 128。
-- ALTER TABLE hawb_raw MODIFY COLUMN SHIPPER_INFO_EORI VARCHAR(128),
--        MODIFY COLUMN CONSIGNEE_INFO_EORI VARCHAR(128);
-- ALTER TABLE hawb_air MODIFY COLUMN SHIPPER_INFO_EORI VARCHAR(128),
--        MODIFY COLUMN CONSIGNEE_INFO_EORI VARCHAR(128);

-- === 老库升级（字段契约 39 -> 40：2026-09-23 之前建的表缺 HS 编码列时执行）===
-- 不升级的话 db_writer 会报 Unknown column 'GOODS_HS_CODE'，失败清单里逐行可见。
-- ALTER TABLE hawb_raw ADD COLUMN GOODS_HS_CODE VARCHAR(64) AFTER GOODS_INFO;
-- ALTER TABLE hawb_air ADD COLUMN GOODS_HS_CODE VARCHAR(64) AFTER GOODS_INFO;
