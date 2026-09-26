# SaaS Storage Management Module

Complete storage management for Odoo SaaS instances.

## Features

### Admin
- View storage for all instances
- Set storage limits
- Approve/reject upgrade requests
- View storage history
- Automatic notifications

### Client
- View own storage in portal
- See usage percentage
- Request storage upgrade
- Email notifications

## Installation

1. Extract module to addons directory
2. Restart Odoo
3. Install module

## Configuration

1. Go to SaaS → Instances
2. Edit instance
3. Set "Storage Limit (GB)" (e.g., 10.0)
4. Save

## Usage

### Admin
- SaaS → Storage Management → Storage History
- View all storage checks

### Client
- Portal → /my/storage
- View storage usage
- Request upgrade

## Cron Job

Runs every 1 hour to check storage and send notifications.

## Notifications

Email alerts sent when storage reaches:
- 80% - Warning
- 90% - Critical
- 100%+ - Full
