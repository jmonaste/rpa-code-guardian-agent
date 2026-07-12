# Process Definition Document — ACME Invoice Processing

## 1. Purpose

Automate the registration of supplier invoices into the ACME invoicing system.

## 2. Process requirements

1. The robot shall read pending invoices from the Orchestrator queue named ACME_Invoices.
2. Each invoice PDF shall be located by its identifier and its number typed into the Invoice Entry screen of the invoicing web application.
3. Failed transactions shall be retried up to 2 times before being marked as Failed.
4. Every system exception shall be logged with the exception message.
5. At the end of the run the robot shall send a summary email to the accounts-payable team.

## 3. Out of scope

Manual invoice approval remains a human task.
