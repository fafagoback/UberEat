/**
 * UberEats Radar - 全域前端配置
 * v9.0: static rollback client. Turso writes run in GitHub Actions.
 */
window.UBER_RADAR_CONFIG = {
  API_BASE_URL: './data', // rollback source until Worker cutover is validated
  // Legacy URLs are kept only for rollback diagnostics during migration.
  ENABLE_DUCKDB: false
};
