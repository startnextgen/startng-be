from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from ... import admin as admins
from ...activity import record
from ...db import get_db
from ...models import Game, User
from ...routers import pages
from ...security import current_user
from ..progress import service as progress
from . import goals as game_goals
from . import service
from .catalog import BY_KEY, CATALOG, RARITIES, SLOTS
from .schemas import (BuyOut, CatalogItemOut, CoinsIn, EntryOut, GoalOut, GrantIn, ItemIn, ItemOut, OwnedOut,
                      PublicWardrobeOut, ShopOut, SlotIn, WalletOut, WardrobeOut)

# main.py mounts this with the feature's flag: every route answers 404 while the flag is off.
router = APIRouter(tags=["wardrobe"])
manage = admins.require("wardrobe.manage")


def _item(key: str, price: int | None, shares: dict[str, float]) -> dict:
    i = BY_KEY[key]
    return {"key": i.key, "name": i.name, "slot": i.slot, "rarity": i.rarity, "how": i.how, "price": price,
            "owned_share": round(shares.get(key, 0.0), 4)}


@router.get("/wardrobe/items", response_model=list[CatalogItemOut])
def catalog(db: Session = Depends(get_db)):
    """Every item, rarest last, with today's shop price (None when it isn't in the shop) and how many players own it."""
    prices = {i.key: p for i, p in service.shop_today()}
    shares = service.owned_share(db)
    items = sorted(CATALOG, key=lambda i: RARITIES.index(i.rarity))
    return [CatalogItemOut(**_item(i.key, prices.get(i.key), shares)) for i in items]


@router.get("/me/wallet", response_model=WalletOut)
def wallet(user: User = Depends(current_user), db: Session = Depends(get_db)):
    entries = [EntryOut(amount=e.amount, reason=e.reason, ref=e.ref, at=e.created_at) for e in service.ledger(db, user.id)]
    return WalletOut(balance=service.balance(db, user.id), earned_today=service.earned_today(db, user.id),
                     daily_cap=service.DAILY_CAP, entries=entries)


def _wardrobe(db: Session, user_id: int) -> WardrobeOut:
    have = service.owned(db, user_id)
    return WardrobeOut(owned=[OwnedOut(key=k, source=s) for k, s in sorted(have.items())],
                       worn=service.worn(db, user_id), style=service.style_overrides(db, user_id))


@router.get("/me/wardrobe", response_model=WardrobeOut)
def my_wardrobe(user: User = Depends(current_user), db: Session = Depends(get_db)):
    """What you own (locked items are everything else in /wardrobe/items), what you wear, and what games will see."""
    return _wardrobe(db, user.id)


@router.post("/me/wardrobe/wear", response_model=WardrobeOut)
def wear(body: ItemIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        service.wear(db, user.id, body.item)
    except service.ShopError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err))
    return _wardrobe(db, user.id)


@router.post("/me/wardrobe/take-off", response_model=WardrobeOut)
def take_off(body: SlotIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    if body.slot not in SLOTS:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, f"slot is one of {', '.join(SLOTS)}")
    service.take_off(db, user.id, body.slot)
    return _wardrobe(db, user.id)


@router.get("/players/{player_id}/wardrobe", response_model=PublicWardrobeOut)
def player_wardrobe(player_id: int, db: Session = Depends(get_db)):
    """Anyone's worn items and their three rarest. 404 for an unknown player or a private page (like their progress)."""
    if progress.public_snapshot(db, player_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such player")
    have = service.owned(db, player_id)
    rarest = sorted(have, key=lambda k: (-RARITIES.index(BY_KEY[k].rarity), k))[:3]
    return PublicWardrobeOut(worn=service.worn(db, player_id), showcase=rarest, items_owned=len(have))


@router.get("/shop", response_model=ShopOut)
def shop(db: Session = Depends(get_db)):
    """Today's shop (it changes at midnight Nigeria time): the featured picks, cheaper, then everything else on sale."""
    stock = service.shop_today()
    shares = service.owned_share(db)
    out = [ItemOut(**_item(i.key, p, shares)) for i, p in stock]
    return ShopOut(day=service.today().isoformat(), featured=out[:service.FEATURED], items=out[service.FEATURED:])


@router.post("/shop/buy", response_model=BuyOut)
def buy(body: ItemIn, user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        left = service.buy(db, user.id, body.item)
    except service.ShopError as err:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, str(err))
    record(db, user, "player", "wardrobe.bought", body.item)
    db.commit()
    return BuyOut(balance=left, item=body.item)


@router.get("/games/{name}/goals", response_model=list[GoalOut])
def goals_of(name: str, db: Session = Depends(get_db)):
    """A game's goals and what each pays today, which start.ng works out from how many of its players reach it."""
    if db.get(Game, name) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such game")
    out = []
    for g in game_goals.of_game(db, name):
        tier, coins, share = game_goals.difficulty(db, g)
        out.append(GoalOut(key=g.key, label=g.label, stat=g.stat, at_least=g.target if g.at_least else None,
                           at_most=None if g.at_least else g.target, repeat="daily" if g.daily else "once",
                           tier=tier, coins=coins, reached_by=round(share, 4)))
    return out


# ---- admin

@router.post("/admin/wardrobe/coins", response_model=WalletOut)
def admin_coins(body: CoinsIn, admin: admins.AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """Add coins, or take them back from a cheater (a negative amount; never below zero)."""
    if db.get(User, body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such player")
    service.adjust(db, body.user_id, body.amount, body.note)
    admins.log(db, admin, "wardrobe.coins", str(body.user_id), amount=body.amount, note=body.note)
    db.commit()
    return wallet(db.get(User, body.user_id), db)


@router.post("/admin/wardrobe/items", response_model=WardrobeOut)
def admin_grant(body: GrantIn, admin: admins.AdminUser = Depends(manage), db: Session = Depends(get_db)):
    """Give a player an item, for example an event's Limited item."""
    if db.get(User, body.user_id) is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, "No such player")
    if body.item not in BY_KEY:
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_ENTITY, "No such item")
    service.grant(db, body.user_id, body.item, source="admin")
    admins.log(db, admin, "wardrobe.item_given", str(body.user_id), item=body.item)
    db.commit()
    return _wardrobe(db, body.user_id)


def _owned_by(share: float) -> str:
    """How many players own an item, in the words of share() in src/features/wardrobe/words.ts."""
    return "Nobody has it yet" if share == 0 else "Owned by under 1% of players" if share < 0.01 else f"Owned by {round(share * 100)}% of players"


@router.get("/pages/shop", response_class=Response, include_in_schema=False)
def shop_page(db: Session = Depends(get_db)):
    """/shop's text for search engines, for inside #root (nginx includes it): today's stock in the same words and classes
    as src/features/wardrobe/ShopPage.tsx, signed out."""
    today = shop(db)

    def tiles(items: list[ItemOut], featured: bool) -> str:
        off = '<span class="tag">20% off</span>' if featured else ""
        return "".join(
            f'<li class="wd-item r-{i.rarity}"><strong>{pages._esc(i.name)}</strong><span class="wd-rarity r-{i.rarity}">{i.rarity}</span>'
            f'<span class="wd-how">{_owned_by(i.owned_share)}</span><span class="wd-price">🪙 {i.price or 0:,}{off}</span></li>'
            for i in items
        )

    body = (
        f'{pages.NAV}<main><section class="container wd-page"><header class="shop-hero"><div><h1>Shop</h1>'
        '<p class="tournament-sub">Spend the coins you earn playing, then wear what you own in every game. '
        "Epic and Legendary items can't be bought: earn them.</p></div>"
        '<span class="shop-restock">New stock every day at midnight, Nigeria time</span></header>'
        f'<h3 class="wd-heading">Featured today</h3><ul class="wd-grid featured">{tiles(today.featured, True)}</ul>'
        f'<h3 class="wd-heading">Everything else</h3><ul class="wd-grid">{tiles(today.items, False)}</ul>'
        f"</section></main>{pages.site_footer()}"
    )
    return Response(body, media_type="text/html; charset=utf-8", headers={"Cache-Control": "public, max-age=60"})
