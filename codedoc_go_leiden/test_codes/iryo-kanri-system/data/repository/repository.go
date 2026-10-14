package repository

import (
	"errors"
	"strconv"

	"example.com/iryo/domain/patient"
	"example.com/iryo/kernel/logging"
)

type Repository[T any] interface {
	Find(id int) (T, error)
	Save(v T) error
}

type SqlPatientRepository struct {
	log  *logging.Logger
	rows map[int]patient.Patient
}

func NewSqlPatientRepository(log *logging.Logger) *SqlPatientRepository {
	return &SqlPatientRepository{log: log, rows: map[int]patient.Patient{}}
}

func (r *SqlPatientRepository) Find(id int) (patient.Patient, error) {
	r.log.Log("検索 " + strconv.Itoa(id))
	p, ok := r.rows[id]
	if !ok {
		return patient.Patient{}, errors.New("患者が見つかりません")
	}
	return p, nil
}

func (r *SqlPatientRepository) Save(p patient.Patient) error {
	r.rows[p.ID] = p
	return nil
}
