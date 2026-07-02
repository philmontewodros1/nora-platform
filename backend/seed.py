from database import SessionLocal, engine, Base
import models

Base.metadata.create_all(bind=engine)
db = SessionLocal()

if db.query(models.Shop).count() == 0:
    kirkos = models.Shop(
        name="Kirkos NOC Depot", phone="+251911000001", area="Kirkos",
        latitude=9.0084, longitude=38.7575,
    )
    bole = models.Shop(
        name="Bole Ghion Gas Point", phone="+251911000002", area="Bole",
        latitude=8.9886, longitude=38.7898,
    )
    db.add_all([kirkos, bole])
    db.commit()
    db.refresh(kirkos)
    db.refresh(bole)

    db.add_all([
        models.StockItem(shop_id=kirkos.id, product="gas", brand="NOC", size="12kg", quantity=14),
        models.StockItem(shop_id=kirkos.id, product="gas", brand="NOC", size="6kg", quantity=5),
        models.StockItem(shop_id=kirkos.id, product="water", brand=None, size="jar", quantity=20),
        models.StockItem(shop_id=bole.id, product="gas", brand="Ghion", size="12kg", quantity=9),
        models.StockItem(shop_id=bole.id, product="water", brand=None, size="jar", quantity=12),
    ])
    db.commit()
    print("Seeded sample shops and stock.")
else:
    print("Shops already exist, skipping shop seed.")

if db.query(models.Rider).count() == 0:
    # telegram_id here is a placeholder — real riders register via @userinfobot
    yohannes = models.Rider(name="Yohannes", phone="+251911000010", telegram_id="111111111")
    selam = models.Rider(name="Selam", phone="+251911000011", telegram_id="222222222")
    db.add_all([yohannes, selam])
    db.commit()
    print("Seeded sample riders (off-duty). Use /riders/{id}/duty?on_duty=true to put them on shift.")
else:
    print("Riders already exist, skipping rider seed.")

db.close()
