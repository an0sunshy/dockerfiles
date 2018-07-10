### Usage:

```bash
docker run -itd --name checker --restart=always \
	-e USCIS_CASE_ID=[USCIS_CASE_ID] \
	-e TELEGRAM_BOT_API=[YOUR BOT API] \
	-e TELEGRAM_ID=[YOUR CHAT ID] \ 
	an0sunshy/USCIS-status-checker
```