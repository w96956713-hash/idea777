# Intake - Hemvist Fastigheter AB

Property management company in Västerås. 1,900 apartments in 41 buildings, 32 staff.

## Problem
We spend too much time processing incoming tenant requests, checking our tenant register, and
assigning cases to the right department. About 120 requests a day arrive through the tenant portal
form, email and a phone line where reception types up notes. Two people in customer service spend
most of their day reading requests, looking up the tenant and building, checking whether a similar
case is already open, deciding the department (maintenance, cleaning, rent & contracts, keys &
locks, disturbances) and the priority, and then creating the case.

Urgent issues (water leaks, no heating in winter, broken entrance doors, fire risk) must reach the
on-call maintenance team within 15 minutes. Today they are sometimes found hours later.

## Systems
- Tenant register and property list: exported nightly as CSV from our property system (Vitec).
- Cases: we want them in a proper case list; today it is a shared spreadsheet.
- Work orders to our maintenance contractor Fastighetsdrift Mälardalen go through their REST API
  (they gave us API documentation and a token).
- SMS to tenants via our SMS provider.

## Rules
- Creating and categorising cases automatically is fine.
- Dispatching a contractor costs money: someone must approve every work order.
- Messages to tenants must be approved for now.
- Never share one tenant's details with another tenant.
- Rent disputes and contract terminations always go to a person in Rent & contracts.

## Goal
Requests categorised and assigned within 5 minutes, urgent issues flagged within 15 minutes,
customer service time on sorting cut by at least half.
