-- One Postgres instance locally; each service owns its own database (separate clusters in production).
CREATE DATABASE gj_identity;
CREATE DATABASE gj_tenant;
CREATE DATABASE gj_partner;
CREATE DATABASE gj_order;
CREATE DATABASE gj_payment;
CREATE DATABASE gj_billing;
CREATE DATABASE gj_pharmacy;
CREATE DATABASE gj_lab;
CREATE DATABASE gj_wholesale;
CREATE DATABASE gj_notification;
CREATE DATABASE gj_bridge;
