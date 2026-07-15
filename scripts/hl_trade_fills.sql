-- Table for flattened Hyperliquid trade fill records.
-- Source record shape:
--   [ "<user_addr>", { coin, px, sz, side, time, startPosition, dir,
--                      closedPnl, hash, oid, crossed, fee, builderFee,
--                      tid, feeToken, builder, twapId } ]
-- array[0] -> user_addr; array[1] JSON object expanded, keys kept as column names.

CREATE TABLE `hl_trade_fills` (
  `id`            BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
  `user_addr`     VARCHAR(42)     NOT NULL,   -- array[0], e.g. 0xdfb2...7070
  `coin`          VARCHAR(32),                -- e.g. "ASTER"
  `px`            DECIMAL(38,18),             -- fill price
  `sz`            DECIMAL(38,18),             -- fill size
  `side`          VARCHAR(4),                 -- "B" / "A"
  `time`          BIGINT,                     -- epoch milliseconds
  `startPosition` DECIMAL(38,18),
  `dir`           VARCHAR(32),                -- e.g. "Open Long"
  `closedPnl`     DECIMAL(38,18),
  `hash`          VARCHAR(66),                -- 0x + 64 hex
  `oid`           BIGINT,                     -- order id
  `crossed`       TINYINT(1),                 -- boolean
  `fee`           DECIMAL(38,18),
  `builderFee`    DECIMAL(38,18),
  `tid`           BIGINT,                     -- trade id
  `feeToken`      VARCHAR(16),                -- e.g. "USDC"
  `builder`       VARCHAR(42),                -- builder address
  `twapId`        BIGINT,                     -- nullable
  PRIMARY KEY (`id`),
  UNIQUE KEY `uk_fill` (`tid`, `user_addr`),  -- one fill per trade per user; makes hourly re-runs idempotent
  KEY `idx_user_addr` (`user_addr`),
  KEY `idx_time` (`time`),
  KEY `idx_coin` (`coin`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4;
