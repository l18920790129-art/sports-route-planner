web: python manage.py migrate --run-syncdb && gunicorn config.wsgi:application --bind 0.0.0.0:$PORT --workers 1 --timeout 300 --graceful-timeout 30
